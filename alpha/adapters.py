# Adapters run trusted workflow code and a separate grader in fresh directories.
# Manifests hash declared code/resources and verify optional pins before execution.
# Only visible inputs/state reach workers; graders receive private checks later.

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
import os
import math
from pathlib import Path
import sys
import subprocess
import tempfile
from typing import Callable

from .accounting import JOB
from .contracts import JSON, ROOT, Variant, compact_json
from .processes import run_process
from .transformations import validate_variants


@dataclass
class Adapter:
    cases: list[JSON]
    variants: list[Variant]
    prompts: dict[str, str]
    fleet: JSON | None
    run_task: Callable
    capabilities: set[str]
    fingerprint: JSON
    scripted: bool = False
    rules: JSON | None = None


def native_runtime() -> JSON:
    python = Path(os.environ.get('ALPHA_PYTHON', str(ROOT / 'work/crewai-venv/bin/python')))
    if not python.is_file():
        return {'available': False}
    code = ('import importlib.metadata as m,json,sys; '
            'print(json.dumps({"python":list(sys.version_info[:3]),"packages":'
            '{d.metadata["Name"]:d.version for d in m.distributions()}}))')
    result = subprocess.run([str(python), '-c', code], capture_output=True, text=True, timeout=15, check=True)
    return {'executable_sha256': sha256(python.read_bytes()).hexdigest(), **json.loads(result.stdout)}


def validate_cases(cases) -> list[JSON]:
    if not isinstance(cases, list) or not cases:
        raise ValueError('Adapter dataset must be a nonempty list.')
    seen = set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get('id'), str) or not case['id'] or case['id'] in seen
                or case.get('split') not in ('discovery', 'holdout') or not isinstance(case.get('input'), dict)):
            raise ValueError('Invalid or duplicate adapter case.')
        seen.add(case['id'])
        case.setdefault('category', case['id'])
        case.setdefault('initial', {})
        if not isinstance(case['initial'], dict) or not isinstance(case.get('evaluation_unit', case['id']), str):
            raise ValueError('Invalid initial state or evaluation unit.')
    return cases


def validate_verdict(value: JSON) -> JSON:
    if (type(value.get('passed')) is not bool or type(value.get('unsafe')) is not bool
            or not isinstance(value.get('checks'), list)
            or any(not isinstance(c, dict) or not isinstance(c.get('name'), str) or type(c.get('passed')) is not bool for c in value['checks'])):
        raise ValueError('Adapter grader returned an invalid verdict.')
    if value['passed'] and (value['unsafe'] or value.get('execution_error') or not all(c['passed'] for c in value['checks'])):
        raise ValueError('Adapter grader returned contradictory evidence.')
    return value


def load_manifest(path: str | Path) -> Adapter:
    path = Path(path).resolve()
    home = path.parent
    manifest = json.loads(path.read_text(encoding='utf-8'))
    required = {'version', 'id', 'name', 'cases', 'variants', 'runner', 'grader', 'files'}
    if not required <= manifest.keys() or manifest['version'] != 1:
        raise ValueError('Invalid adapter manifest.')
    def resource(name):
        candidate = (home / name).resolve()
        if not candidate.is_relative_to(home) or not candidate.is_file():
            raise ValueError('Adapter resources must be files inside its directory.')
        return candidate
    files = manifest['files']
    if not isinstance(files, list) or any(not isinstance(p, str) for p in files) or manifest['cases'] not in files:
        raise ValueError('Declare the dataset and all adapter source files.')
    hashes = {name: sha256(resource(name).read_bytes()).hexdigest() for name in files}
    for name, expected in manifest.get('pins', {}).items():
        if name not in hashes or hashes[name] != expected:
            raise ValueError(f'Pinned adapter source changed: {name}')
    for key in ('runner', 'grader'):
        command = manifest[key]
        if not isinstance(command, list) or len(command) < 2 or command[0] not in ('{python}', '{fleet_python}') or command[1] not in files:
            raise ValueError('Commands must use a declared Python script and a supported interpreter placeholder.')
        if any(not isinstance(a, str) for a in command):
            raise ValueError('Adapter command arguments must be strings.')
    capabilities = set(manifest.get('capabilities', []))
    variants = validate_variants(manifest['variants'], manifest['variants'][0]['roles'], capabilities, manifest.get("rules", {}))
    prompts = manifest.get('prompts', {})
    if not isinstance(prompts, dict) or any(not isinstance(v, str) for v in prompts.values()):
        raise ValueError('Adapter prompts must be a string map.')
    cases = validate_cases(json.loads(resource(manifest['cases']).read_text(encoding='utf-8')))
    descriptor = {'manifest': manifest, 'files': hashes}
    if '{fleet_python}' in (manifest['runner'][0], manifest['grader'][0]):
        descriptor['runtime'] = native_runtime()
    metadata = {'id': manifest['id'], 'name': manifest['name'], 'adapter_sha256': sha256(compact_json(descriptor).encode()).hexdigest(),
                'dataset_description': manifest.get('dataset_description', 'User-supplied development suite'),
                'protocol': 'alpha-command-v1', 'provenance': manifest.get('provenance'), 'capabilities': sorted(capabilities)}
    scripted = manifest.get('scripted', False)
    if type(scripted) is not bool:
        raise ValueError('Adapter scripted flag must be a boolean.')

    async def runner(*, test_case, variant, backend, trial, signal=None, **_):
        from time import perf_counter
        started = perf_counter()
        if any(sha256(resource(name).read_bytes()).hexdigest() != checksum for name, checksum in hashes.items()):
            raise ValueError('Adapter changed after its manifest was loaded.')
        trace, tools, artifact, state, error = [], [], '', {}, None
        def command(key):
            args = manifest[key]
            python = sys.executable if args[0] == '{python}' else os.environ.get('ALPHA_PYTHON', str(ROOT / 'work/crewai-venv/bin/python'))
            return [python, str(resource(args[1])), *args[2:]]
        try:
            with tempfile.TemporaryDirectory(prefix='alpha-world-') as world:
                initial = test_case.get('initial', {})
                for name, content in initial.get('files', {}).items():
                    target = (Path(world) / name).resolve()
                    if not target.is_relative_to(Path(world).resolve()) or not isinstance(content, str):
                        raise ValueError('Invalid initial-world file.')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding='utf-8')
                job = JOB.get() or {}
                payload = {'input': test_case['input'], 'initial': initial, 'variant': variant,
                           'prompts': prompts, 'gateway': job.get('gateway'), 'core_python': sys.executable,
                           'model': getattr(backend, 'requested_model', None)}
                env = {**os.environ, 'PYTHONPATH': os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent.parent), os.environ.get('PYTHONPATH')])), 'OTEL_SDK_DISABLED': 'true', 'CREWAI_TELEMETRY_ENABLED': 'false',
                       'LITELLM_LOCAL_MODEL_COST_MAP': 'True'}
                stdout = await run_process(command('runner'), compact_json(payload), timeout=manifest.get('timeout_seconds', 900), signal=signal, cwd=world, env=env, label='Adapter workflow')
                returned = json.loads(stdout)
                if not isinstance(returned, dict) or not isinstance(returned.get('trace'), list):
                    raise ValueError('Adapter workflow returned invalid evidence.')
                trace, tools = returned['trace'], returned.get('tools', [])
                artifact, state = returned.get('artifact', ''), returned.get('final_state', {})
                if not isinstance(artifact, str) or not isinstance(state, dict) or not isinstance(tools, list):
                    raise ValueError('Adapter artifacts, state, or tool events have invalid types.')
                if returned.get('error'):
                    raise RuntimeError('Adapter workflow failed.')
                seen_steps = set()
                for index, step in enumerate(trace):
                    if (not isinstance(step, dict) or step.get('role') not in variant['roles'] or not isinstance(step.get('context'), dict)
                            or type(step.get('model_calls')) is not int or step['model_calls'] < 0):
                        raise ValueError('Adapter trace does not match the configured roles.')
                    if scripted and (step['model_calls'] != 0 or step.get('usage') is not None):
                        raise ValueError('Scripted adapters cannot report model usage.')
                    step.setdefault('id', f'step-{index}')
                    step.setdefault('parent_ids', [] if index == 0 else [f'step-{index-1}'])
                    step.setdefault('elapsed_ms', 0)
                    if (not isinstance(step['id'], str) or step['id'] in seen_steps
                            or not isinstance(step['parent_ids'], list) or any(p not in seen_steps for p in step['parent_ids'])
                            or type(step['elapsed_ms']) not in (int, float) or not math.isfinite(step['elapsed_ms']) or step['elapsed_ms'] < 0):
                        raise ValueError('Adapter trace must be a topologically ordered acyclic graph with nonnegative timings.')
                    seen_steps.add(step['id'])
                for tool in tools:
                    if (not isinstance(tool, dict) or type(tool.get('chargeable', False)) is not bool
                            or any(p not in seen_steps for p in tool.get('parent_ids', []))
                            or ('cost_usd' in tool and (type(tool['cost_usd']) not in (int, float) or not math.isfinite(tool['cost_usd']) or tool['cost_usd'] < 0))):
                        raise ValueError('Invalid adapter tool event or estimated cost.')
                if not scripted:
                    ledger = getattr(backend, 'controller', None)
                    actual = [a for a in ledger.attempts if a['job'] == job.get('key')] if ledger else []
                    ids = {a['id'] for a in actual}
                    recorded_ids = [s.get('attempt_id') for s in trace if s['model_calls']]
                    if (any(s['model_calls'] not in (0, 1) for s in trace) or len(recorded_ids) != len(set(recorded_ids))
                            or set(recorded_ids) != ids):
                        raise ValueError('Live adapters must use the gateway for every model request.')
            with tempfile.TemporaryDirectory(prefix='alpha-grade-') as grading:
                payload = {'artifact': artifact, 'final_state': state, 'checks': test_case.get('checks', []),
                           'expected': test_case.get('expected')}
                stdout = await run_process(command('grader'), compact_json(payload), timeout=30, signal=signal, cwd=grading, label='Adapter grader')
                verdict = validate_verdict(json.loads(stdout))
        except Exception as exception:
            error = str(exception)
            if not isinstance(state, dict):
                state = {}
            verdict = {'passed': False, 'unsafe': False, 'checks': [{'name': 'Adapter execution', 'passed': False}]}
        return {'case_id': test_case['id'], 'category': test_case['category'], 'split': test_case['split'],
                'evaluation_unit': test_case.get('evaluation_unit', test_case['id']), 'variant': variant['id'],
                'trial': trial, 'trace': trace, 'tools': tools, 'final_state': {'artifact': artifact, **state},
                'verdict': verdict, 'error': error or verdict.get('execution_error'), 'elapsed_ms': (perf_counter() - started) * 1000}

    return Adapter(cases, variants, prompts, metadata, runner, capabilities, descriptor, scripted, manifest.get("rules", {}))


def load_adapter(name: str = 'support', path: str | Path | None = None) -> Adapter:
    if path is not None:
        return load_manifest(path)
    if name == 'support':
        from .domain import load_cases, load_variants
        from .engine import load_prompts, run_case
        return Adapter(load_cases(), load_variants(), load_prompts(), None, run_case,
                       {'models', 'prompt_overrides', 'drop_context', 'deterministic', 'conditional'}, {'builtin': 'support'})
    if name == 'crewai-game-builder':
        from .external_fleet import load_external_fleet, run_external_case
        loaded = load_external_fleet()
        return Adapter(**loaded, run_task=run_external_case, capabilities={'models', 'prompt_overrides'}, fingerprint={'fleet': loaded['fleet'], 'runtime': native_runtime()})
    builtins = {'swarm-handoff': 'swarm-handoff', 'crewai-self-review': 'crewai-self-review'}
    if name in builtins:
        return load_manifest(ROOT / 'integrations' / builtins[name] / 'adapter.json')
    raise ValueError('Unknown fleet. Use --adapter PATH to supply a fleet manifest.')
