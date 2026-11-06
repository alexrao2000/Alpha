# Exercise user-facing commands, saved-report compatibility, journal completeness,
# and the legacy launcher from a different working directory.

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from alpha.contracts import ROOT


class CliTests(unittest.TestCase):
    def test_custom_adapter_cli_resume_restores_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, '-m', 'alpha', 'experiment']
            first = subprocess.run(command + ['--adapter', 'examples/adapter/adapter.json', '--trials', '1', '--out', directory],
                                   cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            journal = (Path(directory)/'progress.jsonl').read_bytes()
            second = subprocess.run(command + ['--resume', directory], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(journal, (Path(directory)/'progress.jsonl').read_bytes())

    def test_fixture_cli_journals_runs_and_leaves_configuration_unchanged(self):
        configuration = (ROOT / 'fleet.config.json').read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-m', 'alpha', 'experiment', '--backend', 'fixture',
                                     '--limit', '4', '--trials', '1', '--concurrency', '3', '--out', directory],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((Path(directory) / 'experiment.json').read_text())
            journal = [json.loads(line) for line in (Path(directory) / 'progress.jsonl').read_text().splitlines()]
            self.assertEqual(len(journal), len(report['runs']))
            key = lambda run: (run['case_id'], run['variant'], run['trial'])
            self.assertEqual(sorted(journal, key=key), sorted(report['runs'], key=key))
            self.assertEqual(json.loads(result.stdout)['selected'], 'without-planner')
            self.assertIn('--- a/fleet.config.json', (Path(directory) / 'proposal.patch').read_text())
            self.assertEqual((ROOT / 'fleet.config.json').read_bytes(), configuration)

    def test_report_command_preserves_historical_json_and_configuration_path(self):
        with tempfile.TemporaryDirectory() as directory:
            original = ROOT / 'examples/upstream-game-builder/experiment.json'
            result = subprocess.run([sys.executable, '-m', 'alpha', 'report', str(original), '--out', directory],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((Path(directory) / 'experiment.json').read_text()), json.loads(original.read_text()))
            self.assertIn('integrations/crewai-game-builder/fleet.config.json', (Path(directory) / 'proposal.patch').read_text())
            self.assertIn('Independent upstream crew', (Path(directory) / 'report.html').read_text())

    @unittest.skipUnless(shutil.which('node'), 'Optional legacy Node launcher')
    def test_legacy_node_launcher_works_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['node', str(ROOT / 'src/cli.mjs'), 'experiment', '--backend', 'fixture',
                                     '--limit', '1', '--trials', '1', '--out', 'result'], cwd=directory,
                                    env={**os.environ, 'ALPHA_CORE_PYTHON': sys.executable}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(directory) / 'result/report.html').exists())
