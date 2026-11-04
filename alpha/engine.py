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
from uuid import uuid4

from .contracts import Backend, Experiment, JSON, ROOT, Run, Step, Usage, Variant, Job, compact_json
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
        before = perf_counter()
        try:
            result = await backend.call(role, prompts[role], deepcopy(context), signal=signal)
            output = validate_output(role, result["output"])
            outputs[role] = deepcopy(output)
            if role == "researcher":
                outputs[role] = {"order": world.lookup(output["order_id"])}
            trace.append({"role": role, "context": context, "output": output,
                          "elapsed_ms": (perf_counter() - before) * 1000,
                          "usage": result["usage"], "model_calls": result["model_calls"]})
        except Exception as exception:
            error = str(exception)
            trace.append({"role": role, "context": context, "error": error,
                          "elapsed_ms": (perf_counter() - before) * 1000, "usage": None, "model_calls": 0})
            break
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
            "final_state": world.state, "verdict": verdict, "error": error, "elapsed_ms": (perf_counter() - started) * 1000}


def summarize(runs: list[Run]) -> JSON:
    usage: list[Usage] = [u for r in runs for step in r["trace"] if (u := step["usage"]) is not None]
    times = sorted(r["elapsed_ms"] for r in runs)
    successes = sum(bool(r["verdict"]["passed"]) for r in runs)
    def tokens(field: str) -> int:
        return sum(cast(int, u.get(field, 0) or 0) for u in usage)
    return {"count": len(runs), "passed": successes, "pass_rate": successes / len(runs) if runs else None,
            "unsafe": sum(bool(r["verdict"]["unsafe"]) for r in runs), "errors": sum(bool(r.get("error")) for r in runs),
            "role_calls": sum(len(r["trace"]) for r in runs),
            "model_calls": sum(s["model_calls"] for r in runs for s in r["trace"]),
            "input_tokens": tokens("input_tokens") if usage else None,
            "cached_input_tokens": tokens("cached_input_tokens") if usage else None,
            "output_tokens": tokens("output_tokens") if usage else None,
            "median_ms": times[len(times) // 2] if times else None,
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
    if not baseline or bs["errors"] or bs["pass_rate"] != 1:
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
                     on_run: Callable | None = None, signal: asyncio.Event | None = None) -> Experiment:
    if type(trials) is not int or trials < 1:
        raise ValueError("Trials must be a positive integer.")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("Limit must be a positive integer.")
    if type(concurrency) is not int or not 1 <= concurrency <= 8:
        raise ValueError("Concurrency must be an integer from 1 to 8.")
    variants = deepcopy(VARIANTS if variants is None else variants)
    if not variants or variants[0]["id"] != "baseline" or len({v["id"] for v in variants}) != len(variants):
        raise ValueError("Variants must start with a unique baseline.")
    discovery = [c for c in cases if c["split"] == "discovery"][:limit]
    holdout = [c for c in cases if c["split"] == "holdout"][:limit]
    if not discovery or not holdout:
        raise ValueError("Both discovery and holdout cases are required.")
    dataset_hash = sha256(compact_json(cases).encode()).hexdigest()
    prompt_hash = sha256(compact_json(prompts).encode()).hexdigest()
    runs: list[Run] = []

    async def execute(selected_cases: list[JSON], selected_variants: list[Variant], phase: str) -> None:
        jobs: list[Job] = []
        for index, case in enumerate(selected_cases):
            for trial in range(trials):
                offset = (index + trial) % len(selected_variants)
                for variant in selected_variants[offset:] + selected_variants[:offset]:
                    jobs.append({"test_case": case, "variant": variant, "trial": trial})
        completed: list[Run | None] = [None] * len(jobs)
        next_index = 0

        async def worker() -> None:
            nonlocal next_index
            while next_index < len(jobs):
                if signal is not None and signal.is_set():
                    raise asyncio.CancelledError("Experiment cancelled.")
                index = next_index
                next_index += 1
                job = jobs[index]
                await notify(on_progress, f"{phase} · {job['test_case']['id']} · {job['variant']['id']} · trial {job['trial'] + 1}")
                completed[index] = await run_task(**deepcopy(job), backend=backend, prompts=deepcopy(prompts), signal=signal)
                await notify(on_run, completed[index])

        workers = [asyncio.create_task(worker()) for _ in range(min(concurrency, len(jobs)))]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker_task in workers:
                if not worker_task.done():
                    worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        if any(run is None for run in completed):
            raise RuntimeError("The runner returned no result.")
        runs.extend(run for run in completed if run is not None)

    await execute(discovery, variants, "Discovery")
    selected = choose_candidate(runs, variants)
    frozen_at = timestamp()
    await execute(holdout, [variants[0], selected] if selected else [variants[0]], "Holdout")
    baseline_holdout = [r for r in runs if r["split"] == "holdout" and r["variant"] == "baseline"]
    selected_holdout = [r for r in runs if r["split"] == "holdout" and selected and r["variant"] == selected["id"]]
    comparison = compare(baseline_holdout, selected_holdout) if selected else None
    hs = summarize(selected_holdout)
    validated = bool(selected and summarize(baseline_holdout)["pass_rate"] == 1
                     and not hs["errors"] and not hs["unsafe"] and comparison is not None and not comparison["regressions"])
    return {"version": 1, "id": str(uuid4()), "created_at": timestamp(), "backend": backend.name,
            "model": backend.model, "fixture": backend.name == "fixture", "trials": trials, "concurrency": concurrency,
            "fleet": fleet, "dataset": {"source": (fleet or {}).get("dataset_description", "Alpha authored support development suite"),
             "sha256": dataset_hash, "discovery_cases": len(discovery), "holdout_cases": len(holdout)},
            "prompt_sha256": prompt_hash, "variants": variants, "selected": selected["id"] if selected else None,
            "frozen_at": frozen_at, "validated": validated, "comparison": comparison, "runs": runs}
