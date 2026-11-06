# These are the original experiment invariants ported to Python, plus compatibility
# checks against the saved JavaScript fixture. Scripted runs never measure models.

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import unittest

from alpha.backends import FixtureBackend
from alpha.contracts import ROOT
from alpha.domain import World, VARIANTS, grade, load_cases, make_variants, role_context, validate_output
from alpha.engine import run_case, load_prompts, experiment, compare, summarize, choose_candidate
from alpha.report import render_report

CASES, PROMPTS, FIXTURE = load_cases(), load_prompts(), FixtureBackend()
DEMO = asyncio.run(experiment(cases=CASES, prompts=PROMPTS, backend=FIXTURE, trials=2))


class ExperimentTests(unittest.IsolatedAsyncioTestCase):
    def test_full_fleet_passes_every_authored_case_across_both_splits(self):
        runs = [r for r in DEMO['runs'] if r['variant'] == 'baseline']
        self.assertEqual(len(runs), 64)
        self.assertEqual(summarize(runs)['pass_rate'], 1)

    def test_discovery_selects_planner_removal_and_frozen_holdout_confirms(self):
        self.assertEqual(DEMO['selected'], 'without-planner')
        self.assertFalse(DEMO['validated'])
        self.assertEqual(DEMO['decision'], 'scripted')
        self.assertTrue(DEMO['assessments'][DEMO['selected']]['functional_passed'])
        self.assertEqual(DEMO['comparison']['regressions'], [])
        selected = [r for r in DEMO['runs'] if r['split'] == 'holdout' and r['variant'] == DEMO['selected']]
        self.assertEqual(len(selected), 32)
        self.assertEqual(summarize(selected)['role_calls'], 128)

    def test_negative_controls_expose_policy_research_and_review_losses(self):
        for variant in ['without-policy', 'without-researcher', 'without-reviewer', 'lean']:
            with self.subTest(variant=variant):
                self.assertLess(summarize([r for r in DEMO['runs'] if r['variant'] == variant])['pass_rate'], 1)
        self.assertGreater(summarize([r for r in DEMO['runs'] if r['variant'] == 'without-reviewer'])['unsafe'], 0)

    async def test_every_candidate_reexecutes_downstream_roles(self):
        calls = []
        class Recording(FixtureBackend):
            async def call(self, role, *args, **kwargs):
                calls.append(role)
                return await super().call(role, *args, **kwargs)
        for variant in (VARIANTS[0], next(v for v in VARIANTS if v['id'] == 'without-planner')):
            await run_case(test_case=CASES[0], variant=variant, backend=Recording(), prompts=PROMPTS)
        self.assertEqual(len(calls), 9)
        self.assertEqual(calls.count('resolver'), 2)

    def test_worlds_and_original_cases_stay_isolated(self):
        before = deepcopy(CASES[0])
        a, b = World(CASES[0]['initial']), World(CASES[0]['initial'])
        a.apply({'action': 'refund', 'amount_cents': 5000}, CASES[0]['input'])
        self.assertEqual(b.state['refunds'], [])
        self.assertEqual(CASES[0], before)
        b.lookup(CASES[0]['input']['order_id'])['status'] = 'cancelled'
        self.assertEqual(b.state['orders'][0]['status'], 'delivered')

    async def test_model_context_excludes_oracles_and_case_metadata(self):
        test = self
        class Inspecting(FixtureBackend):
            async def call(self, role, instruction, context, **kwargs):
                for forbidden in ('expected', 'initial', 'category', 'split', 'case_id'):
                    test.assertNotIn(forbidden, context)
                test.assertNotIn('refund_count', json.dumps(context))
                return await super().call(role, instruction, context, **kwargs)
        run = await run_case(test_case=CASES[0], variant=VARIANTS[0], backend=Inspecting(), prompts=PROMPTS)
        self.assertTrue(run['verdict']['passed'])

    def test_policy_sees_no_planner_order_when_research_is_removed(self):
        self.assertEqual(role_context('policy', CASES[0]['input'], {'planner': {'order': CASES[0]['initial']['orders'][0]}}),
                         {'request': CASES[0]['input'], 'order': None})

    def test_grader_rejects_excess_wrong_target_duplicate_and_wrong_action(self):
        case = CASES[0]
        correct = World(case['initial'])
        correct.apply({'action': 'refund', 'amount_cents': 5000}, case['input'])
        self.assertTrue(grade(correct.state, case['initial'], case['expected'])['passed'])
        states = [deepcopy(correct.state) for _ in range(4)]
        states[0]['refunds'][0]['amount_cents'] = 5001
        states[1]['refunds'][0]['order_id'] = 'wrong-order'
        states[2]['refunds'].append({**states[2]['refunds'][0], 'amount_cents': 0})
        states[3]['ticket']['action'] = 'deny'
        for state in states:
            self.assertFalse(grade(state, case['initial'], case['expected'])['passed'])

    def test_action_tool_does_not_repair_policy(self):
        case = next(c for c in CASES if c['category'] == 'expired-refund')
        world = World(case['initial'])
        world.apply({'action': 'refund', 'amount_cents': 7000}, case['input'])
        self.assertEqual(len(world.state['refunds']), 1)
        self.assertTrue(grade(world.state, case['initial'], case['expected'])['unsafe'])

    async def test_holdout_changes_cannot_change_discovery_winner(self):
        changed = deepcopy(CASES)
        for case in changed:
            if case['split'] == 'holdout':
                case['expected']['refund_cents'] = 123456
        result = await experiment(cases=changed, prompts=PROMPTS, backend=FIXTURE, trials=1)
        self.assertEqual(result['selected'], DEMO['selected'])
        self.assertFalse(result['validated'])

    async def test_provider_failures_are_visible_and_cannot_win(self):
        class Broken(FixtureBackend):
            name, model = 'broken', 'none'
            async def call(self, *args, **kwargs):
                raise RuntimeError('test provider failure')
        result = await experiment(cases=CASES, prompts=PROMPTS, backend=Broken(), trials=1, limit=1)
        self.assertIsNone(result['selected'])
        self.assertFalse(result['validated'])
        self.assertTrue(all(r['error'] for r in result['runs']))

    def test_comparison_refuses_unpaired_duplicate_and_mismatched_trials(self):
        baseline = [r for r in DEMO['runs'] if r['variant'] == 'baseline' and r['split'] == 'discovery']
        candidate = [r for r in DEMO['runs'] if r['variant'] == DEMO['selected'] and r['split'] == 'discovery']
        for first, second in [(baseline, candidate[1:]), (baseline, [candidate[0]] * len(candidate)),
                              ([baseline[0]] * len(baseline), candidate)]:
            with self.assertRaises(ValueError):
                compare(first, second)

    def test_fixture_counts_are_never_model_usage(self):
        summary = summarize(DEMO['runs'])
        self.assertEqual(summary['model_calls'], 0)
        self.assertIsNone(summary['input_tokens'])
        self.assertIsNone(summary['tokens_per_success'])
        self.assertGreater(summary['role_calls'], 0)
        self.assertTrue(DEMO['fixture'])

    def test_selection_uses_measured_tokens_at_equal_quality(self):
        baseline = [r for r in DEMO['runs'] if r['variant'] == 'baseline' and r['split'] == 'discovery']
        variants = [VARIANTS[0], {'id': 'cheap', 'roles': ['resolver']}, {'id': 'expensive', 'roles': ['resolver']}]
        runs = baseline.copy()
        for name, tokens in [('cheap', 10), ('expensive', 100)]:
            runs.extend({**r, 'variant': name, 'trace': [{'model_calls': 1, 'usage': {'input_tokens': tokens, 'output_tokens': tokens // 2}}]} for r in baseline)
        self.assertEqual(choose_candidate(runs, variants)['id'], 'cheap')

    def test_role_configuration_preserves_required_order(self):
        for roles in [[], ['planner'], ['resolver', 'unknown'], ['resolver', 'resolver'], ['reviewer', 'resolver']]:
            with self.assertRaises(ValueError):
                make_variants(roles)
        self.assertEqual(make_variants(['policy', 'resolver'])[0]['roles'], ['policy', 'resolver'])

    def test_malformed_outputs_fail_before_state_mutation(self):
        values = [('resolver', {'action': 'refund', 'amount_cents': -1, 'reason': ''}),
                  ('policy', {'disposition': 'approve', 'max_refund_cents': 0, 'reason': ''}),
                  ('planner', {'steps': [42]}),
                  ('resolver', {'action': 'refund', 'amount_cents': True, 'reason': ''}),
                  ('resolver', {'action': 'refund', 'amount_cents': 2**53, 'reason': ''}),
                  ('researcher', {'order_id': 'x', 'extra': 1})]
        for role, value in values:
            with self.assertRaises(ValueError):
                validate_output(role, value)

    async def test_empty_splits_invalid_trials_and_concurrency_are_rejected(self):
        for options in [{'cases': []}, {'trials': 0}, {'trials': True}, {'limit': -1}, {'concurrency': 9}, {'variants': []}]:
            with self.assertRaises(ValueError):
                await experiment(**({'cases': CASES, 'prompts': PROMPTS, 'backend': FIXTURE} | options))

    async def test_bounded_parallel_execution_preserves_pairing_and_selection(self):
        result = await experiment(cases=CASES, prompts=PROMPTS, backend=FIXTURE, trials=1, limit=4, concurrency=3)
        self.assertEqual(result['selected'], 'without-planner')
        self.assertEqual(result['decision'], 'scripted')
        self.assertEqual(len(result['runs']), 32)

    def test_report_escapes_markup_and_script_terminators(self):
        poisoned = deepcopy(DEMO)
        poisoned['runs'][0]['category'] = '</script><script>alert(1)</script>@@FOOTER@@'
        rendered = render_report(poisoned)
        self.assertNotIn('</script><script>alert(1)</script>', rendered)
        self.assertIn('SCRIPTED DEMO', rendered)
        self.assertIn('Unmeasured', rendered)
        self.assertIn('@@FOOTER@@', rendered)
        embedded = rendered.split('<script type="application/json" id="experiment">')[1].split('</script>')[0]
        self.assertEqual(json.loads(embedded), poisoned)

    def test_report_provenance_does_not_create_executable_links(self):
        poisoned = deepcopy(DEMO)
        poisoned['fleet'] = {'id': 'custom', 'name': 'Custom', 'provenance': {'repository': 'javascript:alert(1)', 'commit': 'fake'}}
        self.assertNotIn('href="javascript:', render_report(poisoned))

    def test_all_fixture_evidence_and_digests_match_saved_javascript_run(self):
        original = json.loads((ROOT / 'examples/fixture/experiment.json').read_text())
        original.setdefault('fleet', None)
        def stable(value):
            if isinstance(value, dict):
                return {k: stable(v) for k, v in value.items() if k != 'elapsed_ms'}
            if isinstance(value, list):
                return [stable(v) for v in value]
            return value
        current = deepcopy(DEMO)
        for old, new in zip(original['runs'], current['runs'], strict=True):
            for step in new['trace']:
                for field in ('id', 'parent_ids', 'model', 'execution', 'attempt_id'):
                    step.pop(field, None)
            new = {k: v for k, v in new.items() if k in old}
            self.assertEqual(stable(old), stable(new))

    async def test_backend_mutation_does_not_change_recorded_context(self):
        class Mutating(FixtureBackend):
            async def call(self, role, instruction, context, **kwargs):
                result = await super().call(role, instruction, context, **kwargs)
                context['request']['customer_id'] = 'modified'
                return result
        run = await run_case(test_case=CASES[0], variant=VARIANTS[0], backend=Mutating(), prompts=PROMPTS)
        self.assertEqual(run['trace'][0]['context']['request'], CASES[0]['input'])

    async def test_cancellation_is_propagated_instead_of_scored(self):
        signal = asyncio.Event()
        signal.set()
        with self.assertRaises(asyncio.CancelledError):
            await experiment(cases=CASES, prompts=PROMPTS, backend=FIXTURE, signal=signal)

    async def test_parallel_work_is_cancelled_when_a_runner_fails(self):
        active = set()
        async def runner(**kwargs):
            token = id(asyncio.current_task())
            active.add(token)
            try:
                if kwargs['variant']['id'] == 'baseline':
                    await asyncio.sleep(0.01)
                    raise RuntimeError('Runner infrastructure failed')
                await asyncio.sleep(30)
            finally:
                active.remove(token)
        with self.assertRaisesRegex(RuntimeError, 'infrastructure'):
            await experiment(cases=CASES, prompts=PROMPTS, backend=FIXTURE, concurrency=3, run_task=runner)
        self.assertFalse(active)
