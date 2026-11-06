# Exercise statistical decisions, accounting failures, actual native control flow,
# and durable resume with a scripted transport. These are engineering checks, never
# live model evaluations. Private expected values never enter worker/model inputs.

import asyncio
from copy import deepcopy
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from alpha.accounting import Controller, BudgetPolicy, BudgetStop, frontier, usage_summary
from alpha.adapters import load_adapter, load_manifest
from alpha.backends import CodexBackend, FixtureBackend
from alpha.contracts import ROOT
from alpha.domain import load_cases, VARIANTS
from alpha.engine import experiment, load_prompts, run_case
from alpha.evaluation import ValidationPolicy, assess, regression_upper
from alpha.session import RunStore, digest
from alpha.transformations import expanded_variants, validate_variants


def evidence(count=40, trials=2, failed=()):
    return [{'case_id': str(i), 'trial': trial, 'verdict': {'passed': i not in failed, 'unsafe': False}, 'error': None}
            for i in range(count) for trial in range(trials)]


def transport(path):
    program = '''
import asyncio,json,re,sys
from pathlib import Path
sys.path.insert(0,ROOT)
from alpha.backends import FixtureBackend
args=sys.argv
prompt=sys.stdin.read()
schema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())
if 'CONTEXT\\n' in prompt:
    role=re.search(r'You are the (\\w+)',prompt).group(1)
    result=asyncio.run(FixtureBackend().call(role,'',json.loads(prompt.split('CONTEXT\\n')[1])))['output']
elif 'content' in schema['properties']:
    data=json.loads(prompt)
    messages=data['messages']
    message=next(m['content'] for m in messages if m['role']=='user')
    spanish='Spanish' in messages[0]['content']
    if 'Hola' in message and not spanish and data.get('tools'):
        result={'content':'','tool_calls':[{'name':'transfer_to_spanish_agent','arguments':'{}'}]}
    else:
        result={'content':message.split(':')[-1].strip().rstrip('.'),'tool_calls':[]}
else:
    messages=json.loads(prompt.split('\\n\\n',1)[1])
    joined=' '.join(m.get('content','') for m in messages)
    if 'review' in messages[0].get('content','').lower() or 'verifier' in messages[0].get('content','').lower():
        response='Final Answer: '+json.dumps({'valid':True,'feedback':None})
    else:
        match=re.search(r"Given the topic '([^']+)'",joined)
        topic=match.group(1) if match else 'Flying cars'
        post=f'Thou, behold {topic}!\\nThe dawn doth bring a wondrous change to town.\\nOur hopeful hearts shall rise above all doubt.\\nLet thoughtful minds make room for kinder days.\\nThus progress earns its place through useful deeds.'
        response='Final Answer: '+post
    result={'response':response}
Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(result))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':3,'output_tokens':4}}))
'''
    path.write_text('#!' + sys.executable + '\nROOT=' + repr(str(ROOT)) + '\n' + program)
    path.chmod(0o755)
    return CodexBackend(model='scripted-test-model', binary=str(path))


class EvaluationTests(unittest.TestCase):
    def test_exact_bound_known_values_and_simultaneous_correction(self):
        self.assertAlmostEqual(regression_upper(0, 100, .05), 1 - .05 ** .01)
        self.assertAlmostEqual(regression_upper(5, 100, .05), .1022533776, places=8)
        self.assertEqual(regression_upper(0, 0, .05), 1)
        self.assertEqual(regression_upper(4, 4, .05), 1)
        a = assess(evidence(), evidence(), ValidationPolicy())
        b = assess(evidence(), evidence(), ValidationPolicy(), comparisons=5)
        self.assertEqual(a['decision'], 'validated')
        self.assertGreater(b['regression_upper_bound'], a['regression_upper_bound'])
        self.assertEqual(b['decision'], 'inconclusive')

    def test_repeats_and_related_cases_never_inflate_independent_units(self):
        baseline = evidence(100, 10)
        candidate = deepcopy(baseline)
        for run in baseline + candidate:
            run['evaluation_unit'] = str(int(run['case_id']) // 10)
        result = assess(baseline, candidate, ValidationPolicy())
        self.assertEqual(result['independent_units'], 10)
        self.assertEqual(result['decision'], 'inconclusive')
        candidate[0]['verdict']['passed'] = False
        result = assess(baseline, candidate, ValidationPolicy())
        self.assertEqual(result['regressed_units'], 1)

    def test_imperfect_baseline_floor_unsafe_errors_and_scripted(self):
        baseline, candidate = evidence(failed=(0,)), evidence(failed=(0,))
        self.assertEqual(assess(baseline, candidate, ValidationPolicy())['decision'], 'validated')
        self.assertEqual(assess(baseline, candidate, ValidationPolicy(minimum_quality=1))['decision'], 'rejected')
        candidate[0]['verdict']['unsafe'] = True
        self.assertEqual(assess(baseline, candidate, ValidationPolicy())['decision'], 'rejected')
        self.assertEqual(assess(baseline, candidate, ValidationPolicy(), scripted=True)['decision'], 'scripted')
        candidate[0]['verdict']['unsafe'] = False
        baseline[0]['error'] = 'execution failed'
        self.assertEqual(assess(baseline, candidate, ValidationPolicy())['decision'], 'inconclusive')


class AccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_admission_never_exceeds_request_cap(self):
        state = {}
        controller = Controller(BudgetPolicy(max_calls=3), state, lambda: None)
        async def operation():
            await asyncio.sleep(.01)
            return {'usage': {'input_tokens': 10, 'output_tokens': 2}, 'output': {}}
        results = await asyncio.gather(*(controller.call(operation, model='m') for _ in range(10)), return_exceptions=True)
        self.assertEqual(len(controller.attempts), 3)
        self.assertEqual(sum(isinstance(r, BudgetStop) for r in results), 7)
        self.assertTrue(all(a['status'] == 'completed' for a in controller.attempts))

    async def test_token_threshold_unknown_usage_failure_and_invalid_usage(self):
        controller = Controller(BudgetPolicy(max_tokens=10), {}, lambda: None)
        async def good():
            return {'usage': {'input_tokens': 10, 'output_tokens': 2}, 'output': {}}
        await controller.call(good, model='m')
        with self.assertRaises(BudgetStop):
            await controller.call(good, model='m')
        controller = Controller(BudgetPolicy(max_tokens=10), {}, lambda: None)
        async def broken():
            raise RuntimeError('provider failed')
        with self.assertRaises(RuntimeError):
            await controller.call(broken, model='m')
        with self.assertRaisesRegex(BudgetStop, 'Unknown usage'):
            await controller.call(good, model='m')
        controller = Controller(BudgetPolicy(), {}, lambda: None)
        async def invalid():
            return {'usage': {'input_tokens': -1, 'output_tokens': 2}}
        with self.assertRaises(ValueError):
            await controller.call(invalid, model='m')
        self.assertIsNone(controller.attempts[0]['usage'])
        self.assertEqual(controller.attempts[0]['status'], 'failed')

    async def test_deadline_and_cancellation_retain_unknown_attempts(self):
        controller = Controller(BudgetPolicy(deadline_seconds=.02), {}, lambda: None)
        async def slow():
            await asyncio.sleep(10)
        with self.assertRaises(BudgetStop):
            await controller.call(slow, model='m')
        self.assertEqual(controller.attempts[0]['status'], 'interrupted')
        self.assertEqual(usage_summary(controller.attempts)['unknown_usage_attempts'], 1)

    def test_cached_prices_partial_unknown_and_frontier(self):
        attempts = [{'status': 'completed', 'model': 'm', 'usage': {'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 20}}]
        prices = {'m': {'input': 2, 'cached_input': .5, 'output': 4}}
        summary = usage_summary(attempts, prices)
        self.assertAlmostEqual(summary['estimated_model_cost_usd'], .00022)
        self.assertIsNone(summary['billing_cost_usd'])
        summary = usage_summary(attempts + [{'status': 'failed', 'usage': None}], prices)
        self.assertIsNone(summary['estimated_model_cost_usd'])
        self.assertAlmostEqual(summary['partial_estimated_model_cost_usd'], .00022)
        self.assertIsNone(usage_summary(attempts, prices, scripted=True)['reported_tokens'])
        rows = [dict(id='a', acceptable=True, quality=1, median_ms=10, estimated_cost_usd=1),
                dict(id='b', acceptable=True, quality=1, median_ms=20, estimated_cost_usd=2),
                dict(id='unknown', acceptable=True, quality=1, median_ms=1, estimated_cost_usd=None),
                dict(id='failed', acceptable=False, quality=1, median_ms=0, estimated_cost_usd=0)]
        self.assertEqual(frontier(rows), ['a', 'unknown'])


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_freezes_discovery_skips_completed_and_rejects_changed_inputs(self):
        cases, prompts = load_cases(), load_prompts()
        signal = asyncio.Event()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            def stop(run):
                if run['split'] == 'holdout':
                    signal.set()
            first = await experiment(cases=cases, prompts=prompts, backend=FixtureBackend(), trials=1, limit=2,
                                     directory=path, signal=signal, on_run=stop, return_partial_on_cancel=True)
            self.assertEqual(first['status'], 'interrupted')
            self.assertTrue(first['frozen_candidates'])
            saved = (path/'progress.jsonl').read_text()
            resumed = await experiment(cases=cases, prompts=prompts, backend=FixtureBackend(), trials=1, limit=2, directory=path, resume=True)
            self.assertEqual(resumed['status'], 'complete')
            self.assertEqual(first['frozen_at'], resumed['frozen_at'])
            self.assertEqual(first['recommendations'], resumed['recommendations'])
            self.assertTrue((path/'progress.jsonl').read_text().startswith(saved))
            completed = (path/'progress.jsonl').read_text()
            await experiment(cases=cases, prompts=prompts, backend=FixtureBackend(), trials=1, limit=2, directory=path, resume=True)
            self.assertEqual(completed, (path/'progress.jsonl').read_text())
            for change in ({'prompts': {**prompts, 'planner': 'changed'}}, {'trials': 2}, {'variants': VARIANTS[:2]},
                           {'policy': ValidationPolicy(confidence=.90)}, {'cases': cases[:-1]}):
                args = dict(cases=cases, prompts=prompts, backend=FixtureBackend(), trials=1, limit=2, directory=path, resume=True)
                with self.assertRaisesRegex(ValueError, 'fingerprint'):
                    await experiment(**(args | change))

    def test_journal_corruption_duplicate_checkpoint_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = RunStore({'code': '1'}, path)
            record = {'case_id': 'one', 'variant': 'baseline', 'trial': 0, 'split': 'discovery'}
            initial = (path/'checkpoint.json').read_text()
            store.complete(record)
            (path/'checkpoint.json').write_text(initial)
            self.assertEqual(len(RunStore({'code': '1'}, path, resume=True).completed), 1)
            journal = (path/'progress.jsonl').read_text()
            (path/'progress.jsonl').write_text(journal.replace('"one"', '"two"'))
            with self.assertRaisesRegex(ValueError, 'altered'):
                RunStore({'code': '1'}, path, resume=True)
            (path/'progress.jsonl').write_text(journal)
            data = json.loads((path/'checkpoint.json').read_text())
            data['state']['status'] = 'tampered'
            (path/'checkpoint.json').write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                RunStore({'code': '1'}, path, resume=True)

    async def test_live_budget_pause_resume_and_accumulated_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = transport(root/'fake-codex')
            options = dict(cases=load_cases(), prompts=load_prompts(), backend=backend, trials=1, limit=1,
                           variants=VARIANTS[:2], directory=root/'run')
            first = await experiment(**options, budget=BudgetPolicy(max_calls=2))
            self.assertEqual(first['status'], 'paused')
            self.assertEqual(first['accounting']['attempted_calls'], 2)
            self.assertEqual(first['runs'], [])
            from alpha.cli import write_outputs
            write_outputs(first, root/'partial-report')
            self.assertIn('PAUSED', (root/'partial-report/report.html').read_text().upper())
            resumed = await experiment(**options, budget=BudgetPolicy(max_calls=30), resume=True)
            self.assertEqual(resumed['status'], 'complete')
            self.assertEqual(resumed['accounting']['attempted_calls'], 20)
            self.assertEqual(len(resumed['accounting']['budget_history']), 1)
            self.assertFalse(resumed['validated'])
            self.assertEqual(resumed['decision'], 'inconclusive')
            with self.assertRaisesRegex(ValueError, 'increases only'):
                await experiment(**options, budget=BudgetPolicy(max_calls=29), resume=True)
            backend.requested_model = backend.model = 'changed-model'
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                await experiment(**options, budget=BudgetPolicy(max_calls=30), resume=True)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_scripted_adapter_has_fresh_state_and_separate_grader(self):
        adapter = load_manifest(ROOT/'examples/adapter/adapter.json')
        result = await experiment(cases=adapter.cases, prompts=adapter.prompts, backend=FixtureBackend(), variants=adapter.variants,
                                  run_task=adapter.run_task, fleet=adapter.fleet, trials=2, adapter_fingerprint=adapter.fingerprint)
        self.assertEqual(result['decision'], 'scripted')
        self.assertTrue(all(r['final_state']['counter'] == 1 for r in result['runs']))
        self.assertTrue(all(r['verdict']['passed'] for r in result['runs'] if r['variant'] == 'baseline'))
        self.assertFalse(any(r['verdict']['passed'] for r in result['runs'] if r['variant'] != 'baseline'))
        self.assertEqual(result['accounting']['attempted_calls'], 0)

    async def test_expanded_recipes_reexecute_and_record_conditional_and_deterministic_steps(self):
        variants = expanded_variants(VARIANTS, support=True, model='explicit-model')
        validate_variants(variants, VARIANTS[0]['roles'], {'models', 'prompt_overrides', 'drop_context', 'deterministic', 'conditional'})
        for name in ('deterministic-review', 'conditional-review', 'combined-review', 'less-resolver-context'):
            variant = next(v for v in variants if v['id'] == name)
            run = await run_case(test_case=load_cases()[0], variant=variant, backend=FixtureBackend(), prompts=load_prompts())
            self.assertTrue(run['verdict']['passed'])
            if name == 'deterministic-review':
                self.assertEqual(run['trace'][-1]['execution'], 'deterministic')
            if name == 'conditional-review':
                self.assertEqual(run['trace'][-1]['execution'], 'condition_skipped')
            if name == 'combined-review':
                self.assertNotIn('planner', [s['role'] for s in run['trace']])
                self.assertNotIn('plan', next(s for s in run['trace'] if s['role'] == 'resolver')['context'])
        invalid = deepcopy(variants[:2])
        invalid[1]['deterministic'] = {'resolver': 'oracle'}
        with self.assertRaises(ValueError):
            validate_variants(invalid, VARIANTS[0]['roles'], {'deterministic'})

    @unittest.skipUnless((ROOT/'work/crewai-venv/bin/python').exists(), 'Requires optional native dependencies')
    async def test_native_swarm_handoff_and_crewai_review_use_gateway_and_original_runtimes(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = transport(Path(directory)/'fake-codex')
            for name in ('swarm-handoff', 'crewai-self-review'):
                with self.subTest(name=name):
                    adapter = load_adapter(name)
                    result = await experiment(cases=adapter.cases, prompts=adapter.prompts, variants=adapter.variants, run_task=adapter.run_task,
                                              fleet=adapter.fleet, backend=backend, adapter_fingerprint=adapter.fingerprint, limit=2, trials=1)
                    self.assertEqual(result['status'], 'complete')
                    self.assertTrue(all(r['error'] is None for r in result['runs']), [(r['error'],r['case_id']) for r in result['runs']])
                    self.assertTrue(all(r['verdict']['passed'] for r in result['runs'] if r['variant'] == 'baseline'), [(r['case_id'],r['verdict']) for r in result['runs']])
                    self.assertEqual(result['accounting']['attempted_calls'], sum(s['model_calls'] for r in result['runs'] for s in r['trace']))
                    if name == 'swarm-handoff':
                        self.assertTrue(any(t['tool'] == 'transfer_to_spanish_agent' for r in result['runs'] for t in r['tools']))
                        self.assertTrue(any(not r['verdict']['passed'] for r in result['runs'] if r['variant'] == 'without-spanish'))
                    else:
                        self.assertEqual(set(result['frozen_candidates']), {'baseline', 'structural-review', 'conditional-review'})
                        self.assertTrue(any(s.get('execution') == 'deterministic' for r in result['runs'] for s in r['trace']))
                    self.assertFalse(result['validated'])

    async def test_role_model_override_uses_host_prices_and_cannot_create_live_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = transport(Path(directory)/'fake-codex')
            variants = expanded_variants(VARIANTS[:1], model='alternative')
            prices = {'scripted-test-model': {'input': 10, 'cached_input': 2, 'output': 20},
                      'alternative': {'input': 1, 'cached_input': .2, 'output': 2}}
            result = await experiment(cases=load_cases(), prompts=load_prompts(), backend=backend, trials=1, limit=1,
                                      variants=variants, objective='estimated-cost', prices=prices)
            self.assertEqual(result['selected'], 'alternate-model')
            self.assertEqual(result['recommendations']['cheapest'], 'alternate-model')
            self.assertTrue(any(a['model'] == 'alternative' and a['role'] == 'reviewer' for a in result['attempts']))
            self.assertTrue(all(a['model'] == 'scripted-test-model' for a in result['attempts'] if a['role'] != 'reviewer'))
            from alpha.cli import write_outputs
            write_outputs(result, Path(directory)/'output')
            self.assertNotIn('--- ', (Path(directory)/'output/proposal.patch').read_text())

    async def test_live_deadline_stops_inflight_provider_and_preserves_unknown_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory)/'slow-codex'
            binary.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(30)\n')
            binary.chmod(0o755)
            result = await experiment(cases=load_cases(), prompts=load_prompts(), backend=CodexBackend(model='test', binary=str(binary)),
                                      trials=1, limit=1, variants=VARIANTS[:2], concurrency=2, budget=BudgetPolicy(deadline_seconds=.15))
            self.assertEqual(result['status'], 'paused')
            self.assertEqual(result['runs'], [])
            self.assertEqual(result['accounting']['interrupted_calls'], 2)
            self.assertEqual(result['accounting']['unknown_usage_attempts'], 2)

    @unittest.skipUnless((ROOT/'work/crewai-venv/bin/python').exists(), 'Requires optional native dependencies')
    async def test_native_gateway_budget_cancels_workers_and_drains_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = load_adapter('swarm-handoff')
            backend = transport(Path(directory)/'fake-codex')
            result = await experiment(cases=adapter.cases, prompts=adapter.prompts, variants=adapter.variants, run_task=adapter.run_task,
                                      fleet=adapter.fleet, backend=backend, adapter_fingerprint=adapter.fingerprint, limit=2, trials=1,
                                      concurrency=3, budget=BudgetPolicy(max_calls=1))
            self.assertEqual(result['status'], 'paused')
            self.assertEqual(result['accounting']['attempted_calls'], 1)
            self.assertTrue(all(a['status'] != 'running' for a in result['attempts']))
