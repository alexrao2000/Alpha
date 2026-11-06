# Commands start or explicitly resume reproducible experiments. Checkpoints and
# append-only traces remain local; no candidate or proposed patch is auto-applied.

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import signal as signals
import sys

from .adapters import load_adapter
from .backends import CodexBackend, FixtureBackend
from .contracts import JSON, compact_json
from .engine import experiment, summarize, timestamp
from .evaluation import ValidationPolicy
from .accounting import BudgetPolicy
from .report import render_report
from .transformations import expanded_variants, validate_variants


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog='alpha', description='Measure the value of each agent step.')
    commands = result.add_subparsers(dest='command')
    commands.add_parser('help')
    commands.add_parser('adapters', help='List bundled fleets and the user adapter entry point')
    run = commands.add_parser('experiment')
    run.add_argument('--backend', choices=('fixture', 'codex'))
    run.add_argument('--fleet', help='support, crewai-game-builder, swarm-handoff, or crewai-self-review')
    run.add_argument('--adapter', type=Path, help='User-supplied adapter.json')
    run.add_argument('--limit', type=int)
    run.add_argument('--trials', type=int)
    run.add_argument('--concurrency', type=int)
    run.add_argument('--variants', help='Comma-separated candidate IDs; baseline is always included')
    run.add_argument('--search', choices=('removal', 'expanded'))
    run.add_argument('--candidate-model', help='Explicit alternative model for a model-routing candidate')
    run.add_argument('--candidates', type=Path, help='JSON list of baseline and arbitrary candidate recipes')
    run.add_argument('--model', help='Pin this for resumable live experiments')
    run.add_argument('--prices', type=Path, help='Per-model input/cached_input/output USD estimates per million tokens')
    run.add_argument('--objective', choices=('tokens', 'estimated-cost', 'latency'))
    run.add_argument('--confidence', type=float)
    run.add_argument('--max-regression-rate', type=float)
    run.add_argument('--min-cases', type=int)
    run.add_argument('--min-trials', type=int)
    run.add_argument('--minimum-quality', type=float)
    run.add_argument('--max-calls', type=int)
    run.add_argument('--max-tokens', type=int, help='Stop new requests after observed tokens reach this threshold; in-flight calls can overshoot')
    run.add_argument('--deadline', type=float, help='Total active experiment seconds, including resumed execution')
    run.add_argument('--resume', type=Path, help='Existing run directory; only budget increases are permitted')
    run.add_argument('--out', type=Path)
    report = commands.add_parser('report')
    report.add_argument('path', type=Path)
    report.add_argument('--out', type=Path)
    return result


def write_outputs(report, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / 'experiment.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    (out / 'report.html').write_text(render_report(report), encoding='utf-8')
    selected = next((v for v in report['variants'] if v['id'] == report['selected']), None)
    baseline = report['variants'][0]
    config = (report.get('fleet') or {}).get('config_path', 'fleet.config.json')
    recipe = [baseline, selected] if selected else [baseline]
    (out / 'candidate-config.json').write_text(json.dumps(recipe, indent=2) + '\n', encoding='utf-8')
    permitted = report.get('version', 1) == 1 or report.get('validated') or report['fixture'] and report.get('status') == 'complete'
    if selected and permitted:
        if set(selected) <= {'id', 'label', 'roles'} and (report.get('fleet') or {}).get('protocol') != 'alpha-command-v1':
            proposal = f"--- a/{config}\n+++ b/{config}\n@@ -1 +1 @@\n-{compact_json(baseline['roles'])}\n+{compact_json(selected['roles'])}\n"
        else:
            lines = json.dumps(recipe, indent=2).splitlines()
            proposal = f'--- /dev/null\n+++ b/candidate-config.json\n@@ -0,0 +1,{len(lines)} @@\n' + ''.join('+' + line + '\n' for line in lines)
    else:
        proposal = 'No validated change proposed. candidate-config.json records the provisional discovery choice only.\n'
    (out / 'proposal.patch').write_text(proposal, encoding='utf-8')


async def run(args) -> int:
    if args.command == 'report':
        report = json.loads(args.path.read_text(encoding='utf-8'))
        out = (args.out or Path('runs') / timestamp().replace(':', '-').replace('.', '-')).resolve()
    else:
        if args.resume and args.out:
            raise ValueError('Use --resume or --out, not both.')
        out = (args.resume or args.out or Path('runs') / timestamp().replace(':', '-').replace('.', '-')).resolve()
        previous = {}
        saved_budget = {}
        if args.resume:
            checkpoint = json.loads((out / 'checkpoint.json').read_text(encoding='utf-8'))
            previous = checkpoint['state'].get('cli', {})
            saved_budget = checkpoint['state']['budget']
        def option(name, fallback=None):
            current = getattr(args, name)
            return current if current is not None else previous.get(name, fallback)
        adapter_path = option('adapter')
        if adapter_path:
            adapter_path = Path(adapter_path).resolve()
        fleet_name = option('fleet', 'support')
        adapter = load_adapter(fleet_name, adapter_path)
        backend_name = option('backend', 'fixture' if adapter.scripted or fleet_name == 'support' and not adapter_path else 'codex')
        if adapter.scripted and backend_name != 'fixture' or not adapter.scripted and (adapter_path or fleet_name != 'support') and backend_name != 'codex':
            raise ValueError('Scripted adapters require fixture mode; native model fleets require codex mode.')
        backend = FixtureBackend() if backend_name == 'fixture' else CodexBackend(model=option('model'))
        variants = adapter.variants
        search = option('search', 'removal')
        if search == 'expanded':
            variants = expanded_variants(variants, model=option('candidate_model'), support=fleet_name == 'support' and not adapter_path)
        candidates_path = option('candidates')
        if candidates_path:
            candidates_path = Path(candidates_path).resolve()
            variants = json.loads(Path(candidates_path).read_text(encoding='utf-8'))
        variants = validate_variants(variants, adapter.variants[0]['roles'], adapter.capabilities, adapter.rules)
        chosen = option('variants')
        if chosen:
            ids = {'baseline', *chosen.split(',')}
            if ids - {v['id'] for v in variants}:
                raise ValueError('Unknown variant ID.')
            variants = [v for v in variants if v['id'] in ids]
        prices_path = option('prices')
        if prices_path:
            prices_path = Path(prices_path).resolve()
        prices = json.loads(Path(prices_path).read_text(encoding='utf-8')) if prices_path else None
        if option('objective', 'tokens') == 'estimated-cost' and not prices:
            raise ValueError('The estimated-cost objective requires --prices with explicit model rates.')
        policy = ValidationPolicy(confidence=option('confidence', .95), max_regression_rate=option('max_regression_rate', .10),
                                  min_cases=option('min_cases', 20), min_trials=option('min_trials', 2), minimum_quality=option('minimum_quality', 0.0))
        budget = BudgetPolicy(args.max_calls if args.max_calls is not None else saved_budget.get('max_calls'),
                              args.max_tokens if args.max_tokens is not None else saved_budget.get('max_tokens'),
                              args.deadline if args.deadline is not None else saved_budget.get('deadline_seconds'))
        cancellation = asyncio.Event()
        loop, installed = asyncio.get_running_loop(), []
        for sig in (signals.SIGINT, signals.SIGTERM):
            try:
                loop.add_signal_handler(sig, cancellation.set)
                installed.append(sig)
            except NotImplementedError:
                pass
        configuration: JSON = {key: str(option(key)) if isinstance(option(key), Path) else option(key) for key in
                         ('adapter', 'fleet', 'backend', 'limit', 'trials', 'concurrency', 'variants', 'search', 'candidate_model',
                          'candidates', 'model', 'prices', 'objective', 'confidence', 'max_regression_rate', 'min_cases', 'min_trials', 'minimum_quality')}
        configuration.update(backend=backend_name, fleet=fleet_name, trials=option('trials', 2), concurrency=option('concurrency', 1),
                             search=search, objective=option('objective', 'tokens'), confidence=policy.confidence,
                             max_regression_rate=policy.max_regression_rate, min_cases=policy.min_cases,
                             min_trials=policy.min_trials, minimum_quality=policy.minimum_quality)
        configuration.update(adapter=str(adapter_path) if adapter_path else None,
                             candidates=str(candidates_path) if candidates_path else None, prices=str(prices_path) if prices_path else None)
        try:
            report = await experiment(cases=adapter.cases, prompts=adapter.prompts, backend=backend, fleet=adapter.fleet,
                                      run_task=adapter.run_task, variants=variants, trials=configuration['trials'],
                                      limit=option('limit'), concurrency=configuration['concurrency'],
                                      signal=cancellation, on_progress=lambda message: print(message, file=sys.stderr),
                                      on_run=lambda run: print(f"Completed · {run['case_id']} · {run['variant']} · {'PASS' if run['verdict']['passed'] else 'FAIL'}", file=sys.stderr),
                                      policy=policy, budget=budget, prices=prices, directory=out, resume=bool(args.resume),
                                      adapter_fingerprint=adapter.fingerprint, objective=configuration['objective'],
                                      run_config=configuration, return_partial_on_cancel=True,
                                      scripted=adapter.scripted or backend_name == 'fixture')
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)
    write_outputs(report, out)
    runs = [r for r in report['runs'] if r['split'] == 'holdout' and r['variant'] == (report['selected'] or 'baseline')]
    print(json.dumps({'backend': report['backend'], 'selected': report['selected'], 'validated': report['validated'],
                      'decision': report.get('decision', 'legacy'), 'status': report.get('status', 'complete'),
                      'holdout': summarize(runs), 'report': str(out / 'report.html')}, indent=2))
    if report.get('status') == 'interrupted':
        return 130
    if report.get('status') == 'paused':
        return 3
    return 2 if any(r.get('error') for r in report['runs']) else 0


def main(argv=None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    if args.command in (None, 'help'):
        arguments.print_help()
        return 0
    if args.command == 'adapters':
        print('support (scripted or live), crewai-game-builder, swarm-handoff, crewai-self-review\nUser fleets: experiment --adapter path/to/adapter.json')
        return 0
    try:
        return asyncio.run(run(args))
    except (asyncio.CancelledError, KeyboardInterrupt):
        print('Alpha: Experiment cancelled; completed evidence remains resumable.', file=sys.stderr)
        return 130
    except Exception as error:
        print(f'Alpha: {error}', file=sys.stderr)
        return 1
