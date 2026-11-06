# Port the external adapter checks and exercise the actual native crew with a
# scripted model transport. These checks make no provider requests or savings claims.

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from alpha.backends import CodexBackend
from alpha.contracts import ROOT
from alpha.engine import experiment
from alpha.external_fleet import HOME, load_external_fleet, run_external_case
from alpha.report import render_report

PROGRAM = '''def solve(request):
    board = request['board']
    lines = board + list(zip(*board)) + [[board[i][i] for i in range(3)], [board[i][2-i] for i in range(3)]]
    for line in lines:
        if line[0] and all(cell == line[0] for cell in line): return line[0]
    return 'draw' if all(cell for row in board for cell in row) else 'ongoing'
'''


def fake_cli(path, response='Final Answer: example', extra=''):
    source = f'''#!{sys.executable}
import json,sys
from pathlib import Path
prompt=sys.stdin.read()
args=sys.argv
assert '--ignore-user-config' in args and '--ephemeral' in args
assert args[args.index('--sandbox')+1]=='read-only'
Path(args[args.index('--output-last-message')+1]).write_text(json.dumps({{'response':{response!r}}}))
print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':10,'output_tokens':4}}}}))
{extra}
'''
    path.write_text(source)
    path.chmod(0o755)


class ExternalFleetTests(unittest.IsolatedAsyncioTestCase):
    def test_source_is_pinned_and_games_are_held_out(self):
        fleet = load_external_fleet()
        self.assertEqual(len(fleet['fleet']['provenance']['commit']), 40)
        self.assertEqual(len(fleet['variants'][0]['roles']), 3)
        self.assertEqual(fleet['fleet']['provenance']['runtime']['version'], '0.152.0')
        discovery = {c['category'] for c in fleet['cases'] if c['split'] == 'discovery'}
        self.assertTrue(all(c['category'] not in discovery for c in fleet['cases'] if c['split'] == 'holdout'))

    async def test_transport_preserves_response_usage_and_rejects_cli_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'fake-codex'
            fake_cli(binary)
            backend = CodexBackend(binary=str(binary))
            result = await backend.complete(schema={'type': 'object'}, prompt='Test transport.')
            self.assertEqual(result['output']['response'], 'Final Answer: example')
            self.assertEqual(result['usage']['input_tokens'], 10)
            for action in ('command_execution', 'mcp_tool_call', 'web_search', 'file_change', 'collab_tool_call'):
                fake_cli(binary, extra=f"print(json.dumps({{'type':'item.completed','item':{{'type':{action!r}}}}}))")
                with self.assertRaisesRegex(RuntimeError, 'external tool'):
                    await backend.complete(schema={'type': 'object'}, prompt='Test transport.')

    async def test_runner_and_journal_receive_every_fresh_variant(self):
        fleet, calls, journal = load_external_fleet(), [], []
        async def runner(*, test_case, variant, trial, **kwargs):
            calls.append((test_case['id'], variant['id']))
            return {'case_id': test_case['id'], 'split': test_case['split'], 'category': test_case['category'],
                    'trial': trial, 'variant': variant['id'], 'trace': [{'role': r, 'model_calls': 1,
                    'usage': {'input_tokens': 10, 'output_tokens': 1}} for r in variant['roles']],
                    'verdict': {'passed': True, 'unsafe': False, 'checks': []}, 'error': None,
                    'elapsed_ms': 1, 'tools': [], 'final_state': {}}
        backend = CodexBackend(model='test')
        result = await experiment(**fleet, backend=backend, trials=1, concurrency=3, run_task=runner, on_run=journal.append)
        self.assertEqual(len(result['runs']), 24)
        self.assertEqual(len(calls), 24)
        self.assertEqual(len(journal), 24)
        self.assertFalse(result['validated'])
        self.assertEqual(result['decision'], 'inconclusive')
        self.assertTrue(all(r['variant'] in result['frozen_candidates'] for r in result['runs'] if r['split'] == 'holdout'))

    def test_external_report_preserves_code_grading_and_provenance(self):
        for path in ['examples/live/experiment.json', 'examples/upstream-game-builder/experiment.json']:
            sample = json.loads((ROOT / path).read_text())
            sample['fleet'] = load_external_fleet()['fleet']
            rendered = render_report(sample)
            self.assertIn('Independent upstream crew', rendered)
            self.assertIn(sample['fleet']['provenance']['commit'], rendered)
            self.assertIn('Hidden input/output checks', rendered)
            self.assertNotIn('Actual disposition, ticket state', rendered)

    def test_tampered_upstream_fails_before_execution(self):
        original = Path.read_bytes
        def tamper(path):
            content = original(path)
            return content + b'changed' if path.name == 'crew.py' else content
        with patch.object(Path, 'read_bytes', tamper), self.assertRaisesRegex(ValueError, 'Upstream source changed'):
            load_external_fleet()

    async def test_private_checks_are_sent_only_to_grader(self):
        fleet, payloads = load_external_fleet(), []
        async def fake_worker(python, script, data, signal, timeout):
            payloads.append((script.name, deepcopy(data)))
            if script.name == 'worker.py':
                return {'trace': [], 'artifact': 'def solve(r): return None', 'tasks': [], 'error': None}
            return {'passed': True, 'unsafe': False, 'checks': [], 'results': []}
        with patch('alpha.external_fleet.run_python', fake_worker), patch('alpha.external_fleet.Path.is_file', return_value=True):
            await run_external_case(test_case=fleet['cases'][0], variant=fleet['variants'][0], backend=CodexBackend(), trial=0)
        self.assertNotIn('checks', payloads[0][1])
        self.assertNotIn('expected', json.dumps(payloads[0][1]))
        self.assertNotIn('node', payloads[0][1])
        self.assertEqual(payloads[0][1]['python'], sys.executable)
        self.assertEqual(payloads[1][1]['checks'], fleet['cases'][0]['checks'])

    @unittest.skipUnless((ROOT / 'work/crewai-venv/bin/python').exists() and sys.platform == 'darwin', 'Requires installed native crew and macOS sandbox')
    async def test_native_crew_and_real_grader_work_without_node(self):
        fleet = load_external_fleet()
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'fake-codex'
            fake_cli(binary, response='Final Answer: ```python\n' + PROGRAM + '\n```')
            backend = CodexBackend(binary=str(binary))
            for variant in fleet['variants']:
                result = await run_external_case(test_case=fleet['cases'][0], variant=variant, backend=backend, trial=0)
                self.assertIsNone(result['error'], result['error'])
                self.assertTrue(result['verdict']['passed'])
                self.assertEqual(len(result['trace']), len(variant['roles']))
                self.assertEqual(len(result['tools']), len(variant['roles']))
                self.assertTrue(all(step['usage']['input_tokens'] == 10 for step in result['trace']))
                self.assertTrue(all('checks' not in step['context'] for step in result['trace']))
