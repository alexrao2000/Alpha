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
from .qualitative import CodexJudge, evaluate, human_review_template, calibrate_saved
from .human_review import render_human_review
from .rubrics import load_rubric, available_rubrics
from .orchestration import demo as orchestration_demo
from .worker_runtime import RunStore, load_identities, run_workers


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog='alpha', description='Measure the value of each agent step.')
    commands = result.add_subparsers(dest='command')
    commands.add_parser('help')
    commands.add_parser('adapters', help='List bundled fleets and the user adapter entry point')
    commands.add_parser('rubrics', help='List bundled and operator-defined grading rubric names')
    orch = commands.add_parser('orch', help='Run live agent tasks with durable worker scheduling (or demo)')
    orch.add_argument('mode', choices=('demo', 'run', 'resume', 'status', 'resolve'))
    orch.add_argument('task', nargs='?', help='Task prompt for run, or task ID for resolve')
    orch.add_argument('--scenario', choices=('all', 'starvation', 'cycles', 'routing', 'side-effects', 'skills', 'limits'),
                      default='all', help='Which orchestration failure mode to demonstrate')
    orch.add_argument('--out', type=Path, help='Write the full JSON trace to this path')
    orch.add_argument('--verbose', action='store_true', help='Print task details or demo events')
    orch.add_argument('--db', type=Path, help='SQLite state path (required for resume/status)')
    orch.add_argument('--agents', type=Path, help='Identity JSON with per-agent instructions and skill paths')
    orch.add_argument('--command', help='External agent command reading JSON on stdin and writing JSON on stdout')
    orch.add_argument('--read-only-command', action='store_true',
                      help='Assert that the external command cannot have side effects; enables safe retries')
    orch.add_argument('--decision', choices=('done', 'failed', 'retry'), help='Resolution for a needs_review task')
    orch.add_argument('--result', help='Confirmed result text when resolving an uncertain task as done')
    orch.add_argument('--identity', help='Root agent identity; defaults to general or the first configured')
    orch.add_argument('--workers', type=int, default=2, help='Concurrent real agent calls')
    orch.add_argument('--tiers', help='Comma-separated worker capacities: small,medium,large')
    orch.add_argument('--model', help='Model to use for all task sizes (default: Codex CLI default)')
    orch.add_argument('--model-small')
    orch.add_argument('--model-medium')
    orch.add_argument('--model-large')
    orch.add_argument('--lease', type=float, default=120, help='Claim lease duration in seconds')
    orch.add_argument('--max-depth', type=int, default=3)
    orch.add_argument('--max-children', type=int, default=4)
    orch.add_argument('--max-tasks', type=int, default=40)
    orch.add_argument('--max-rounds', type=int, default=2)
    orch.add_argument('--max-attempts', type=int, default=2)
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
    qualitative = commands.add_parser('qualitative', help='Run opt-in, post-hoc quality evaluations over a saved experiment')
    qualitative.add_argument('path', type=Path, help='Existing experiment.json')
    qualitative.add_argument('--out', type=Path, help='Output directory (default: experiment directory)')
    qualitative.add_argument('--candidate', help='Candidate variant to compare; defaults to saved selection')
    qualitative.add_argument('--turn-count', action='store_true', help='Count observed user and assistant conversation turns')
    qualitative.add_argument('--feedback', action='store_true', help='Summarize observed satisfaction, corrections, and follow-ups')
    qualitative.add_argument('--interactions', type=Path, help='JSON with observed per-run conversation messages and feedback')
    qualitative.add_argument('--simulate-feedback', action='store_true', help='Tautological scripted example; not empirical user feedback')
    qualitative.add_argument('--human-template', action='store_true', help='Write a blank human scoring and comparison template')
    qualitative.add_argument('--human', type=Path, help='Filled-in human review JSON')
    qualitative.add_argument('--llm-judge', action='store_true', help='Ask a model to score individual holdout outputs')
    qualitative.add_argument('--pairwise', action='store_true', help='Blindly compare candidate and baseline holdout outputs')
    qualitative.add_argument('--judge-model', help='Explicit Codex model; without it uses CLI default')
    qualitative.add_argument('-r', '--rubric', help='Rubric name (e.g. writing), ID, or JSON file path')
    qualitative.add_argument('--max-judgments', type=int, default=12, help='Maximum additional model calls, shared between judge methods')
    quick = commands.add_parser('q', help='Shortcuts for optional evaluations of the latest saved run')
    quick.add_argument('mode', choices=('demo', 'judge', 'pair', 'human', 'turns', 'all', 'calibrate'),
                       help='demo=synthetic feedback, judge=LLM scores+pairwise, pair=pairwise only, human=review template, turns=real conversations, all=all demos')
    quick.add_argument('path', type=Path, nargs='?', help='Experiment JSON or run directory (default: latest in ./runs)')
    quick.add_argument('--out', type=Path, help='Destination for qualitative files (default: run directory)')
    quick.add_argument('--candidate', help='Override the selected candidate')
    quick.add_argument('--interactions', type=Path, help='Observed conversation/feedback records for turns or all')
    quick.add_argument('--human', type=Path, help='Completed human-review JSON for human or all')
    quick.add_argument('--judge-results', type=Path, help='Previously saved qualitative.json containing LLM judgments (calibrate only)')
    quick.add_argument('--judge-model', help='Codex judge model; defaults to the current CLI model')
    quick.add_argument('-r', '--rubric', help='Rubric name (e.g. writing), ID, or JSON file path')
    quick.add_argument('--max-judgments', type=int, default=12, help='Cap on additional LLM judging calls')
    return result



def resolve_experiment_path(path: Path | None) -> Path:
    """Find a saved experiment without guessing agent steps or creating a run."""
    if path is None:
        files = [p for p in Path('runs').glob('*/experiment.json') if p.is_file()]
        if not files:
            raise ValueError('No saved experiments in ./runs; pass a run directory or experiment.json.')
        path = max(files, key=lambda p: (p.stat().st_mtime_ns, str(p)))
    elif path.is_dir():
        path = path / 'experiment.json'
    if not path.is_file():
        raise ValueError(f'No experiment.json found at {path}.')
    return path


def expand_quick_command(args: argparse.Namespace) -> argparse.Namespace:
    """Translate short presets into the existing independent feature switches."""
    if args.command != 'q':
        return args
    mode = args.mode
    if mode == 'calibrate':
        if args.human is None:
            raise ValueError('Calibration needs human ratings: pass --human FILE.')
        if args.interactions is not None:
            raise ValueError('Calibration reads saved judgments, not interaction records.')
        path = resolve_experiment_path(args.path)
        return argparse.Namespace(
            command='calibrate', path=path, out=args.out, human=args.human,
            judge_results=args.judge_results or path.resolve().parent / 'qualitative.json')
    if args.judge_results is not None:
        raise ValueError('--judge-results is only supported by q calibrate.')
    if mode == 'turns' and args.interactions is None:
        raise ValueError('Turn counts need real messages: pass --interactions FILE, or use q demo.')
    if mode in ('judge', 'pair', 'demo') and args.interactions is not None:
        raise ValueError('Use q turns or q all when supplying --interactions.')
    if mode not in ('human', 'all') and args.human is not None:
        raise ValueError('Use q human or q all to load a completed human review.')
    return argparse.Namespace(
        command='qualitative',
        path=resolve_experiment_path(args.path),
        out=args.out,
        candidate=args.candidate,
        turn_count=mode in ('turns', 'all'),
        feedback=mode in ('turns', 'all'),
        interactions=args.interactions,
        simulate_feedback=mode in ('demo', 'all') and args.interactions is None,
        human_template=mode in ('human', 'all') and args.human is None,
        human=args.human,
        llm_judge=mode in ('judge', 'all'),
        pairwise=mode in ('judge', 'pair', 'all'),
        judge_model=args.judge_model,
        rubric=args.rubric,
        max_judgments=args.max_judgments,
    )


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
    if args.command == 'calibrate':
        path = resolve_experiment_path(args.path)
        report = json.loads(path.read_text(encoding='utf-8'))
        human = json.loads(args.human.read_text(encoding='utf-8'))
        judge_results = json.loads(args.judge_results.read_text(encoding='utf-8'))
        result = calibrate_saved(report, judge_results, human)
        out = (args.out or path.resolve().parent).resolve()
        out.mkdir(parents=True, exist_ok=True)
        (out / 'calibration.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        (out / 'calibration-report.html').write_text(
            render_report({**report, 'qualitative': result}), encoding='utf-8')
        print(json.dumps({
            'calibration': str(out / 'calibration.json'),
            'report': str(out / 'calibration-report.html'),
            'rating_matches': result['calibration']['ratings']['matched'],
            'pairwise_matches': result['calibration']['pairwise']['matched'],
            'new_model_calls': 0,
        }, indent=2))
        return 0
    if args.command == 'qualitative':
        path = resolve_experiment_path(args.path)
        report = json.loads(path.read_text(encoding='utf-8'))
        rubric = load_rubric(args.rubric)
        out = (args.out or path.resolve().parent).resolve()
        out.mkdir(parents=True, exist_ok=True)
        if args.human_template:
            template = human_review_template(report, args.candidate, rubric=rubric)
            (out / 'human-review-template.json').write_text(
                json.dumps(template, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
            (out / 'human-review.html').write_text(
                render_human_review(report, template), encoding='utf-8')
            if not any((args.simulate_feedback, args.turn_count, args.feedback,
                        args.llm_judge, args.pairwise, args.human)):
                # A blank questionnaire is not an evaluation. Do not overwrite
                # previously saved LLM judgments needed for calibration.
                print(json.dumps({'human_template': str(out / 'human-review-template.json'),
                                  'human_review_page': str(out / 'human-review.html'),
                                  'model_judgment_calls': 0}, indent=2))
                return 0
        interactions = json.loads(args.interactions.read_text(encoding='utf-8')) if args.interactions else None
        human = json.loads(args.human.read_text(encoding='utf-8')) if args.human else None
        judge = CodexJudge(args.judge_model, rubric=rubric) if args.llm_judge or args.pairwise else None
        qualitative = await evaluate(
            report, interactions=interactions, simulate_feedback=args.simulate_feedback,
            human=human, judge=judge, score=args.llm_judge, pairwise=args.pairwise,
            turn_count=args.turn_count, feedback=args.feedback,
            candidate=args.candidate, max_judgments=args.max_judgments, rubric=rubric)
        (out / 'qualitative.json').write_text(
            json.dumps(qualitative, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
            encoding='utf-8')
        (out / 'qualitative-report.html').write_text(
            render_report({**report, 'qualitative': qualitative}), encoding='utf-8')
        print(json.dumps({'qualitative': str(out / 'qualitative.json'),
                          'report': str(out / 'qualitative-report.html'),
                          'human_template': str(out / 'human-review-template.json') if args.human_template else None,
                          'human_review_page': str(out / 'human-review.html') if args.human_template else None,
                          'methods': qualitative['methods'],
                          'model_judgment_calls': qualitative['model_judgment_calls']}, indent=2))
        return 0
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
    if args.command == 'orch':
        if args.mode == 'demo':
            result = orchestration_demo(args.scenario)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
            if not args.verbose:
                result = {**result, 'results': {
                    name: {k: v for k, v in scenario.items() if k != 'events'}
                    for name, scenario in result['results'].items()}}
            print(json.dumps(result, indent=2))
            return 0
        try:
            if args.mode != 'run' and not args.db:
                raise ValueError('Pass --db to resume, resolve, or inspect a run.')
            db = args.db or Path('runs') / ('orch-' + timestamp().replace(':', '-').replace('.', '-') + '.sqlite')
            store = RunStore(db)
            if args.mode == 'run':
                if not args.task:
                    raise ValueError('Pass a task prompt: alpha orch run "your task".')
                identities = load_identities(args.agents)
                identity = args.identity or ('general' if 'general' in identities else next(iter(identities)))
                limits = dict(max_depth=args.max_depth, max_children=args.max_children,
                              max_tasks=args.max_tasks, max_rounds=args.max_rounds,
                              max_attempts=args.max_attempts)
                if any(not isinstance(x, int) or x < 1 for x in limits.values()):
                    raise ValueError('Delegation and retry limits must be positive integers.')
                store.start(args.task, identity, identities, limits,
                            command=args.command, read_only_command=args.read_only_command)
            if args.mode == 'resolve':
                if not args.task or not args.decision:
                    raise ValueError('Resolve requires a task ID and --decision done|failed|retry.')
                store.resolve(args.task, args.decision, args.result)
            if args.mode in ('run', 'resume'):
                tiers = args.tiers.split(',') if args.tiers else None
                models = {size: (getattr(args, 'model_' + size) or args.model)
                          for size in ('small', 'medium', 'large')}
                result = asyncio.run(run_workers(store, workers=args.workers, tiers=tiers,
                                                 models=models, lease_seconds=args.lease))
            else:
                result = store.status()
            root = next(t for t in result['tasks'] if t['id'] == result['root'])
            summary = dict(database=str(db), root=result['root'], state=root['state'],
                           result=json.loads(root['result']) if root['result'] else None,
                           error=root['error'], counts=result['counts'])
            if args.verbose:
                summary['tasks'] = result['tasks']
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0 if args.mode == 'status' or root['state'] == 'done' else 2
        except (ValueError, OSError, KeyError) as error:
            print(f'Alpha: {error}', file=sys.stderr)
            return 1
    if args.command == 'rubrics':
        for entry in available_rubrics():
            aliases = ', '.join(entry['aliases'])
            display = entry['name'] + (f' ({aliases})' if aliases else '')
            location = entry['path'] or 'built-in'
            print(f'{display}  ·  {entry["id"]}  ·  {location}')
        return 0
    try:
        args = expand_quick_command(args)
        return asyncio.run(run(args))
    except (asyncio.CancelledError, KeyboardInterrupt):
        print('Alpha: Experiment cancelled; completed evidence remains resumable.', file=sys.stderr)
        return 130
    except Exception as error:
        print(f'Alpha: {error}', file=sys.stderr)
        return 1
