"""Short qualitative CLI presets and latest-run resolution."""

from pathlib import Path
import json
import os
import tempfile
import unittest

from alpha.cli import expand_quick_command, parser, resolve_experiment_path


class QuickQualitativeCLITests(unittest.TestCase):
    def test_presets_only_enable_requested_methods(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment.json"
            path.write_text("{}", encoding="utf-8")
            cases = {
                "demo": (True, False, False, False, False),
                "judge": (False, True, True, False, False),
                "pair": (False, False, True, False, False),
                "human": (False, False, False, True, False),
                "turns": (False, False, False, False, True),
                "all": (True, True, True, True, True),
            }
            for mode, expected in cases.items():
                with self.subTest(mode=mode):
                    argv = ["q", mode, str(path)]
                    if mode == "turns":
                        argv += ["--interactions", "conversations.json"]
                    args = expand_quick_command(parser().parse_args(argv))
                    actual = (args.simulate_feedback, args.llm_judge, args.pairwise,
                              args.human_template, args.turn_count)
                    self.assertEqual(actual, expected)
                    self.assertEqual(args.path, path)
                    self.assertEqual(args.command, "qualitative")

    def test_custom_model_budget_and_human_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment.json"
            path.write_text("{}", encoding="utf-8")
            args = expand_quick_command(parser().parse_args([
                "q", "all", str(path), "--judge-model", "example-model",
                "--max-judgments", "2", "--interactions", "observed.json",
                "--human", "review.json",
            ]))
            self.assertFalse(args.simulate_feedback)
            self.assertTrue(args.turn_count)
            self.assertTrue(args.llm_judge)
            self.assertTrue(args.pairwise)
            self.assertFalse(args.human_template)
            self.assertEqual(args.human, Path("review.json"))
            self.assertEqual(args.judge_model, "example-model")
            self.assertEqual(args.max_judgments, 2)

    def test_turn_count_requires_actual_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "interactions"):
                expand_quick_command(parser().parse_args(["q", "turns", str(path)]))

    def test_latest_run_uses_mtime_and_never_chooses_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path.cwd()
            try:
                os.chdir(directory)
                first = Path("runs/first/experiment.json")
                second = Path("runs/second/experiment.json")
                first.parent.mkdir(parents=True)
                second.parent.mkdir(parents=True)
                first.write_text("{}", encoding="utf-8")
                second.write_text("{}", encoding="utf-8")
                os.utime(first, ns=(1000000000, 1000000000))
                os.utime(second, ns=(2000000000, 2000000000))
                Path("runs/incomplete").mkdir()
                self.assertEqual(resolve_experiment_path(None), second)
                self.assertEqual(resolve_experiment_path(first.parent), first)
                args = expand_quick_command(parser().parse_args(["q", "demo"]))
                self.assertEqual(args.path, second)
                os.utime(first, ns=(3000000000, 3000000000))
                self.assertEqual(resolve_experiment_path(None), first)
            finally:
                os.chdir(old)

    def test_missing_run_is_explained(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path.cwd()
            try:
                os.chdir(directory)
                with self.assertRaisesRegex(ValueError, "No saved experiments"):
                    resolve_experiment_path(None)
            finally:
                os.chdir(old)

    def test_existing_qualitative_command_is_unchanged(self):
        args = parser().parse_args([
            "qualitative", "saved/experiment.json", "--pairwise", "--max-judgments", "1"
        ])
        self.assertEqual(expand_quick_command(args), args)
        self.assertTrue(args.pairwise)
        self.assertEqual(args.max_judgments, 1)


if __name__ == "__main__":
    unittest.main()
