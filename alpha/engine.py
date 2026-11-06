# Discovery alone selects the candidate. Holdout begins after selection is frozen;
# every workflow reruns fresh state. Summary fields preserve version-one reports.

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import inspect
from time import perf_counter
from typing import Any, Awaitable, Callable, cast
from pathlib import Path
import platform
import shutil
import sys
from contextlib import AsyncExitStack
import math
from uuid import uuid4

from .contracts import Backend, Experiment, JSON, ROOT, Run, Step, Usage, Variant, Job, ModelResult, compact_json
from .accounting import JOB, BudgetPolicy, BudgetStop, Controller, usage_summary, frontier, validate_prices
from .evaluation import ValidationPolicy, assess, discovery_eligible
from .session import RunStore, digest, job_key
from .transformations import deterministic_review, needs_review
from .domain import VARIANTS, ROLES, World, grade, role_context, validate_output


def load_prompts() -> dict[str, str]:
    return {role: (ROOT / f"agent/subagents/{role}/instructions.md").read_text(encoding="utf-8") for role in ROLES}


async def run_case(*, test_case: JSON, variant: Variant, backend: Backend, prompts: dict[str, str],
                   trial: int = 0, signal: asyncio.Event | None = None) -> Run:
    world = World(test_case["initial"])
    outputs: JSON = {}
    trace: list[Step] = []
    started, error = perf_counter(), None
    for role in variant["roles"]:
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError("Experiment cancelled.")
        context = role_context(role, deepcopy(test_case["input"]), outputs)
        for key in variant.get("drop_context", {}).get(role, []):
            context.pop(key, None)
        instructions = variant.get("prompt_overrides", {}).get(role, prompts[role])
        model_backend = backend.for_model(variant["models"][role]) if role in variant.get("models", {}) and hasattr(backend, "for_model") else backend
        execution = "scripted" if backend.name == "fixture" else "model"
        step_id = f"step-{len(trace)}"
        scope = JOB.set({**(JOB.get() or {}), "role": role})
        before = perf_counter()
        result: ModelResult
        try:
            if role in variant.get("deterministic", {}):
                result = {"output": deterministic_review(context), "usage": None, "model_calls": 0}
                execution = "deterministic"
            elif role in variant.get("conditional", {}) and not needs_review(context):
                result = {"output": deepcopy(context["proposal"]), "usage": None, "model_calls": 0}
                execution = "condition_skipped"
            else:
                result = await model_backend.call(role, instructions, deepcopy(context), signal=signal)
            output = validate_output(role, result["output"])
            outputs[role] = deepcopy(output)
            if role == "researcher":
                outputs[role] = {"order": world.lookup(output["order_id"])}
            recorded: Step = {"role": role, "context": context, "output": output,
                          "id": step_id, "parent_ids": [trace[-1]["id"]] if trace else [],
                          "execution": execution, "model": getattr(model_backend, "requested_model", None),
                          "elapsed_ms": (perf_counter() - before) * 1000,
                          "usage": result["usage"], "model_calls": result["model_calls"]}
            if 'attempt_id' in result:
                recorded['attempt_id'] = result['attempt_id']
            trace.append(recorded)
        except BudgetStop:
            raise
        except Exception as exception:
            error = str(exception)
            trace.append({"role": role, "context": context, "error": error,
                          "elapsed_ms": (perf_counter() - before) * 1000, "usage": None, "model_calls": 0})
            break
        finally:
            JOB.reset(scope)
    if error is None:
        try:
            proposal = outputs.get("reviewer", outputs.get("resolver"))
            if proposal is None:
                raise ValueError("Missing resolution.")
            world.apply(proposal, test_case["input"])
        except Exception as exception:
            error = str(exception)
    verdict = grade(world.state, test_case["initial"], test_case["expected"])
    if error is not None:
        verdict["passed"] = False
    return {"case_id": test_case["id"], "category": test_case["category"], "split": test_case["split"],
            "variant": variant["id"], "trial": trial, "trace": trace, "tools": world.events,
            "evaluation_unit": str(test_case.get("evaluation_unit", test_case["id"])),
            "final_state": world.state, "verdict": verdict, "error": error, "elapsed_ms": (perf_counter() - started) * 1000}


def summarize(runs: list[Run]) -> JSON:
    usage: list[Usage] = []
    attempts = []
    for run in runs:
        if "attempts" in run:
            attempts.extend(run["attempts"])
            usage.extend(a["usage"] for a in run["attempts"] if a.get("usage") is not None)
        else:
            usage.extend(u for step in run["trace"] if (u := step.get("usage")) is not None)
    times = sorted(r["elapsed_ms"] for r in runs)
    def quantile(q):
        if not times:
            return None
        position = (len(times) - 1) * q
        low = math.floor(position)
        return times[low] + (times[math.ceil(position)] - times[low]) * (position - low)
    successes = sum(bool(r["verdict"]["passed"]) for r in runs)
    def tokens(field: str) -> int:
        return sum(cast(int, u.get(field, 0) or 0) for u in usage)
    return {"attempted_calls": len(attempts) if attempts else sum(step["model_calls"] for run in runs for step in run["trace"]),
            "unknown_usage_attempts": sum(a.get("usage") is None for a in attempts), "count": len(runs), "passed": successes, "pass_rate": successes / len(runs) if runs else None,
            "unsafe": sum(bool(r["verdict"]["unsafe"]) for r in runs), "errors": sum(bool(r.get("error")) for r in runs),
            "role_calls": sum(len(r["trace"]) for r in runs),
            "model_calls": sum(s["model_calls"] for r in runs for s in r["trace"]),
            "input_tokens": tokens("input_tokens") if usage else None,
            "cached_input_tokens": tokens("cached_input_tokens") if usage else None,
            "output_tokens": tokens("output_tokens") if usage else None,
            "median_ms": quantile(0.5), "p90_ms": quantile(0.9), "p95_ms": quantile(0.95),
            "tokens_per_success": (tokens("input_tokens") + tokens("output_tokens")) / successes if usage and successes else None}


def compare(baseline: list[Run], candidate: list[Run]) -> JSON:
    paired = {(r["case_id"], r["trial"]): r for r in baseline}
    if len(candidate) != len(baseline):
        raise ValueError("Cannot compare unpaired trials.")
    if len(paired) != len(baseline):
        raise ValueError("Cannot compare duplicate baseline trials.")
    regressions, improvements = [], []
    for run in candidate:
        key = (run["case_id"], run["trial"])
        original = paired.pop(key, None)
        if original is None:
            raise ValueError("Cannot compare different case IDs or trials.")
        label = f"{key[0]}:{key[1]}"
        if original["verdict"]["passed"] and not run["verdict"]["passed"]:
            regressions.append(label)
        if not original["verdict"]["passed"] and run["verdict"]["passed"]:
            improvements.append(label)
    return {"regressions": regressions, "improvements": improvements}


def choose_candidate(results: list[Run], variants: list[Variant]) -> Variant | None:
    baseline = [r for r in results if r["variant"] == "baseline"]
    bs = summarize(baseline)
    if not baseline:
        return None
    eligible = []
    for variant in variants:
        if variant["id"] == "baseline":
            continue
        runs = [r for r in results if r["variant"] == variant["id"]]
        summary = summarize(runs)
        if len(runs) == len(baseline) and not summary["errors"] and not summary["unsafe"] and not compare(baseline, runs)["regressions"]:
            eligible.append((summary["tokens_per_success"] if summary["tokens_per_success"] is not None else summary["role_calls"], variant["id"], variant))
    return min(eligible, key=lambda item: (item[0], item[1]))[2] if eligible else None


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def notify(callback: Callable | None, value: Any) -> None:
    if callback is not None:
        result = callback(value)
        if inspect.isawaitable(result):
            await result


async def experiment(*, cases: list[JSON], backend: Backend, prompts: dict[str, str],
                     trials: int = 2, limit: int | None = None, variants: list[Variant] | None = None,
                     concurrency: int = 1, run_task: Callable[..., Awaitable[Run]] = run_case,
                     fleet: JSON | None = None, on_progress: Callable | None = None,
                     on_run: Callable | None = None, signal: asyncio.Event | None = None,
                     policy: ValidationPolicy | None = None, budget: BudgetPolicy | None = None,
                     prices: JSON | None = None, directory: Path | None = None, resume: bool = False,
                     adapter_fingerprint: JSON | None = None, objective: str = 'tokens',
                     run_config: JSON | None = None, return_partial_on_cancel: bool = False,
                     scripted: bool | None = None) -> Experiment:
    if type(trials) is not int or trials < 1:
        raise ValueError('Trials must be a positive integer.')
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError('Limit must be a positive integer.')
    if type(concurrency) is not int or not 1 <= concurrency <= 8:
        raise ValueError('Concurrency must be an integer from 1 to 8.')
    if objective not in ('tokens', 'estimated-cost', 'latency'):
        raise ValueError('Unknown optimization objective.')
    policy, budget, prices = policy or ValidationPolicy(), budget or BudgetPolicy(), validate_prices(prices)
    scripted = backend.name == 'fixture' if scripted is None else scripted
    variants = deepcopy(VARIANTS if variants is None else variants)
    if not variants or variants[0]['id'] != 'baseline' or len({v['id'] for v in variants}) != len(variants):
        raise ValueError('Variants must start with a unique baseline.')
    discovery = [c for c in cases if c['split'] == 'discovery'][:limit]
    holdout = [c for c in cases if c['split'] == 'holdout'][:limit]
    if not discovery or not holdout or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Both discovery and holdout need unique cases.')
    if {c.get('evaluation_unit', c['id']) for c in discovery} & {c.get('evaluation_unit', c['id']) for c in holdout}:
        raise ValueError('Discovery and holdout evaluation units overlap.')
    if resume and backend.name == 'codex' and getattr(backend, 'requested_model', None) is None:
        raise ValueError('Pin --model before starting a resumable live experiment; CLI defaults are not stable identities.')
    if resume and run_task is not run_case and adapter_fingerprint is None:
        raise ValueError('Resumable custom runners require an adapter fingerprint.')
    core = Path(__file__).parent
    code = {str(p.relative_to(core)): sha256(p.read_bytes()).hexdigest()
            for p in sorted([*core.glob('*.py'), *core.glob('templates/*.html')])}
    executable = getattr(backend, 'binary', None)
    binary_path = shutil.which(executable) if executable else None
    identity = {'name': backend.name, 'model': backend.model, 'timeout': getattr(backend, 'timeout', None),
                'binary_sha256': sha256(Path(binary_path).read_bytes()).hexdigest() if binary_path else None}
    runner_code = inspect.getsource(run_task) if inspect.isfunction(run_task) else type(run_task).__qualname__
    plan = {'version': 2, 'dataset_sha256': digest(cases), 'prompt_sha256': digest(prompts),
            'variants': variants, 'backend': identity, 'policy': policy.to_dict(), 'prices': prices,
            'trials': trials, 'limit': limit, 'concurrency': concurrency, 'objective': objective,
            'scripted': scripted, 'code': code, 'adapter': adapter_fingerprint or fleet,
            'runner_sha256': sha256(runner_code.encode()).hexdigest(),
            'python': list(sys.version_info[:3]), 'platform': platform.system()}
    store = RunStore(plan, directory, resume=resume)
    state = store.state
    state.setdefault('id', str(uuid4()))
    state.setdefault('created_at', timestamp())
    if run_config is not None:
        state['cli'] = run_config
    old_budget = state.get('budget')
    if old_budget is not None and old_budget != budget.to_dict():
        for key, before in old_budget.items():
            after = budget.to_dict()[key]
            if before is None and after is not None or before is not None and after is not None and after < before:
                raise ValueError('Resume permits budget increases only.')
        state.setdefault('budget_history', []).append({'previous': old_budget, 'new': budget.to_dict(), 'at': timestamp()})
    state['budget'] = budget.to_dict()
    control = Controller(budget, state, store.save, prices)
    actual_backend = backend
    from .backends import CodexBackend
    if isinstance(backend, CodexBackend):
        actual_backend = CodexBackend(model=backend.requested_model, binary=backend.binary, timeout=backend.timeout, controller=control)
    store.save()

    def jobs_for(selected_cases, selected_variants):
        jobs = []
        for index, case in enumerate(selected_cases):
            for trial in range(trials):
                offset = (index + trial) % len(selected_variants)
                for variant in selected_variants[offset:] + selected_variants[:offset]:
                    jobs.append({'test_case': case, 'variant': variant, 'trial': trial})
        return jobs

    def ordered_runs():
        selected_variants = [v for v in variants if v['id'] in (state.get('frozen') or {}).get('candidates', ['baseline'])]
        jobs = jobs_for(discovery, variants) + jobs_for(holdout, selected_variants)
        return [store.completed[key] for j in jobs if (key := job_key(j['test_case']['id'], j['variant']['id'], j['trial'])) in store.completed]

    def measurements(runs, acceptable_ids):
        rows = []
        for variant in variants:
            selected_runs = [r for r in runs if r['variant'] == variant['id']]
            if not selected_runs:
                continue
            summary = summarize(selected_runs)
            attempts = [a for r in selected_runs for a in r.get('attempts', [])]
            usage = usage_summary(attempts, prices, scripted=scripted)
            tokens_per_success = summary['tokens_per_success'] if not attempts or usage['usage_complete'] else None
            tool_values = [t.get('cost_usd') for r in selected_runs for t in r['tools'] if t.get('chargeable')]
            tool_cost = sum(tool_values) if tool_values and all(type(x) in (int, float) and x >= 0 for x in tool_values) else None
            cost = usage['estimated_model_cost_usd']
            if tool_values:
                cost = None if tool_cost is None or cost is None else cost + tool_cost
            rows.append({'id': variant['id'], 'label': variant['label'], 'quality': summary['pass_rate'],
                         'median_ms': summary['median_ms'], 'p95_ms': summary['p95_ms'],
                         'tokens_per_success': tokens_per_success,
                         'estimated_cost_usd': cost / summary['passed'] if cost is not None and summary['passed'] else None,
                         'model_cost_estimate_usd': usage['estimated_model_cost_usd'], 'tool_cost_usd': tool_cost,
                         'unsafe': summary['unsafe'], 'errors': summary['errors'], 'usage': usage,
                         'acceptable': variant['id'] in acceptable_ids})
        return rows

    async def execute(selected_cases, selected_variants, phase, broker):
        jobs = [j for j in jobs_for(selected_cases, selected_variants)
                if job_key(j['test_case']['id'], j['variant']['id'], j['trial']) not in store.completed]
        next_index = 0
        async def worker():
            nonlocal next_index
            while next_index < len(jobs):
                if signal is not None and signal.is_set():
                    raise asyncio.CancelledError('Experiment cancelled.')
                remaining = control.remaining()
                if remaining is not None and remaining <= 0:
                    control.stop('Active experiment deadline reached.')
                index = next_index
                next_index += 1
                job = jobs[index]
                key = job_key(job['test_case']['id'], job['variant']['id'], job['trial'])
                info = {'key': key, 'variant': job['variant']}
                gateway = broker.lease(info) if broker is not None else None
                token = JOB.set({**info, 'gateway': gateway})
                try:
                    await notify(on_progress, f"{phase} · {job['test_case']['id']} · {job['variant']['id']} · trial {job['trial'] + 1}")
                    operation = run_task(**deepcopy(job), backend=actual_backend, prompts=deepcopy(prompts), signal=signal)
                    remaining = control.remaining()
                    if remaining is None:
                        result = await operation
                    else:
                        async with asyncio.timeout(remaining):
                            result = await operation
                    if control.stop_reason:
                        raise BudgetStop(control.stop_reason)
                    result['evaluation_unit'] = job['test_case'].get('evaluation_unit', job['test_case']['id'])
                    attempts = [deepcopy(a) for a in control.attempts if a['job'] == key]
                    if attempts:
                        result['attempts'] = attempts
                    store.complete(result)
                    await notify(on_run, result)
                except TimeoutError:
                    remaining = control.remaining()
                    if remaining is not None and remaining <= 0:
                        control.stop('Active experiment deadline reached.')
                    raise
                finally:
                    JOB.reset(token)
                    if gateway:
                        broker.release(gateway['lease'])
        workers = [asyncio.create_task(worker()) for _ in range(min(concurrency, len(jobs)))]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    cancelled = False
    try:
        async with AsyncExitStack() as stack:
            broker = None
            if isinstance(actual_backend, CodexBackend) and run_task is not run_case:
                from .broker import ModelBroker
                broker = await stack.enter_async_context(ModelBroker(actual_backend))
            if state.get('frozen') is None:
                await execute(discovery, variants, 'Discovery', broker)
                discovery_runs = [r for r in state['runs'] if r['split'] == 'discovery']
                baseline = [r for r in discovery_runs if r['variant'] == 'baseline']
                eligible = [v['id'] for v in variants[1:] if discovery_eligible(baseline, [r for r in discovery_runs if r['variant'] == v['id']], policy)]
                rows = measurements(discovery_runs, ['baseline', *eligible])
                candidates = [r for r in rows if r['id'] in eligible]
                def metric(row, field):
                    value = row.get(field)
                    return (math.inf if value is None else value, row['id'])
                cost_field = {'tokens': 'tokens_per_success', 'estimated-cost': 'estimated_cost_usd', 'latency': 'median_ms'}[objective]
                usable = [r for r in candidates if r.get(cost_field) is not None or scripted]
                if scripted and objective == 'tokens':
                    primary = choose_candidate(discovery_runs, variants)
                    selected = primary['id'] if primary else None
                else:
                    selected = min(usable, key=lambda r: metric(r, cost_field))['id'] if usable else None
                ranked = [r for r in rows if r['id'] in eligible or r['id'] == 'baseline']
                cheapest_field = 'estimated_cost_usd' if prices else 'tokens_per_success'
                cheapest = [r for r in ranked if r.get(cheapest_field) is not None]
                recommendations = {'cheapest': min(cheapest, key=lambda r: metric(r, cheapest_field))['id'] if cheapest else None,
                                   'fastest': min(ranked, key=lambda r: metric(r, 'median_ms'))['id'],
                                   'highest_quality': min(ranked, key=lambda r: (-r['quality'], r['id']))['id']}
                store.freeze({'selected': selected, 'candidates': ['baseline', *eligible],
                              'recommendations': recommendations, 'frozen_at': timestamp(),
                              'discovery_sha256': digest(discovery_runs)})
            frozen = state['frozen']
            await execute(holdout, [v for v in variants if v['id'] in frozen['candidates']], 'Holdout', broker)
            state['status'] = 'complete'
    except BudgetStop as exception:
        state['status'], control.stop_reason = 'paused', str(exception)
    except asyncio.CancelledError:
        state['status'] = 'interrupted'
        cancelled = True
    except Exception:
        state['status'] = 'failed'
        raise
    finally:
        control.snapshot()
    if cancelled and not return_partial_on_cancel:
        raise asyncio.CancelledError('Experiment cancelled.')
    runs = ordered_runs()
    frozen = state.get('frozen') or {'selected': None, 'candidates': [], 'recommendations': {}, 'frozen_at': None}
    assessments = {}
    baseline = [r for r in runs if r['split'] == 'holdout' and r['variant'] == 'baseline']
    for variant in variants[1:]:
        if variant['id'] not in frozen['candidates']:
            continue
        candidate = [r for r in runs if r['split'] == 'holdout' and r['variant'] == variant['id']]
        if state['status'] == 'complete':
            assessments[variant['id']] = assess(baseline, candidate, policy, comparisons=len(frozen['candidates']) - 1, scripted=scripted)
    selected = frozen['selected']
    assessment = assessments.get(selected, {})
    decision = assessment.get('decision', 'scripted' if scripted else 'inconclusive')
    acceptable = ['baseline', *[key for key, value in assessments.items() if value['decision'] == 'validated' or scripted and value['functional_passed']]]
    rows = measurements([r for r in runs if r['split'] == 'holdout'], acceptable)
    comparison = compare(baseline, [r for r in runs if r['split'] == 'holdout' and r['variant'] == selected]) if selected and state['status'] == 'complete' else None
    return {'version': 2, 'id': state['id'], 'created_at': state['created_at'], 'backend': backend.name,
            'model': backend.model, 'fixture': scripted, 'trials': trials, 'concurrency': concurrency, 'fleet': fleet,
            'dataset': {'source': (fleet or {}).get('dataset_description', 'Alpha authored support development suite'),
                        'sha256': plan['dataset_sha256'], 'discovery_cases': len(discovery), 'holdout_cases': len(holdout)},
            'prompt_sha256': plan['prompt_sha256'], 'variants': variants, 'selected': selected,
            'frozen_at': frozen['frozen_at'], 'validated': decision == 'validated' and state['status'] == 'complete',
            'comparison': comparison, 'runs': runs, 'decision': decision, 'status': state['status'],
            'stop_reason': control.stop_reason, 'policy': policy.to_dict(), 'assessments': assessments,
            'frozen_candidates': frozen['candidates'], 'recommendations': frozen['recommendations'],
            'tradeoffs': rows, 'frontier': frontier(rows), 'accounting': {**usage_summary(control.attempts, prices, scripted=scripted),
              'active_seconds': control.elapsed(), 'budget': budget.to_dict(), 'budget_history': state.get('budget_history', [])},
            'attempts': deepcopy(control.attempts), 'manifest_sha256': store.fingerprint}
