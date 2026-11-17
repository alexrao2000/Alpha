"""Tests for the 2x2 human-vs-model judgment matrix and calibration."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from alpha.calibration import analyze_judgments
from alpha.qualitative import calibrate_saved, human_review_template, run_key
from alpha.cli import parser, expand_quick_command
from alpha.report import judgment_analysis_html


def sample_report():
    runs = []
    for name in ("one", "two"):
        for variant in ("baseline", "candidate"):
            runs.append({
                "case_id": name, "split": "holdout", "trial": 0, "variant": variant,
                "trace": [], "final_state": {"artifact": name}, "verdict": {"passed": True},
            })
    return {
        "id": "experiment-123", "selected": "candidate",
        "variants": [{"id": "baseline"}, {"id": "candidate"}], "runs": runs,
    }


def pair_keys(report, name):
    runs = [r for r in report["runs"] if r["case_id"] == name]
    return run_key(runs[0]), run_key(runs[1])


class CalibrationTests(unittest.TestCase):
    def test_matrix_and_matched_agreement(self):
        human_scores = [
            {"key": "one", "score": 4},
            {"key": "two", "score": 1},
            {"key": "human-only", "score": 3},
        ]
        llm_scores = [
            {"key": "one", "score": 5},
            {"key": "two", "score": 3},
            {"key": "model-only", "score": 4},
        ]
        human_pairs = [
            {"baseline_key": "b1", "candidate_key": "c1", "preferred": "baseline"},
            {"baseline_key": "b2", "candidate_key": "c2", "preferred": "tie"},
        ]
        llm_pairs = [
            {"baseline_key": "b1", "candidate_key": "c1", "preferred": "candidate"},
            {"baseline_key": "b2", "candidate_key": "c2", "preferred": "tie"},
        ]
        result = analyze_judgments(llm_scores, llm_pairs, human_scores, human_pairs)
        matrix = result["matrix"]
        self.assertEqual(matrix["human"]["stars"]["count"], 3)
        self.assertEqual(matrix["llm"]["stars"]["count"], 3)
        self.assertEqual(matrix["human"]["ab"]["count"], 2)
        self.assertEqual(matrix["llm"]["ab"]["count"], 2)
        self.assertEqual(matrix["human"]["ab"]["preferences"]["tie"], 1)
        ratings = result["calibration"]["ratings"]
        self.assertEqual(ratings["matched"], 2)
        self.assertEqual(ratings["exact_agreement"], 0)
        self.assertEqual(ratings["within_one_star"], 0.5)
        self.assertEqual(ratings["mean_absolute_error"], 1.5)
        self.assertEqual(ratings["mean_signed_error"], 1.5)
        pair = result["calibration"]["pairwise"]
        self.assertEqual(pair["matched"], 2)
        self.assertEqual(pair["exact_agreement"], 0.5)
        self.assertEqual(pair["confusion"]["tie"]["tie"], 1)
        self.assertEqual(len(pair["disagreements"]), 1)
        html = judgment_analysis_html(result)
        self.assertIn("2 × 2", html)
        self.assertIn("Human ↔ LLM", html)
        self.assertIn("50.0%", html)

    def test_missing_reference_is_unavailable_not_zero_agreement(self):
        result = analyze_judgments(
            [{"key": "x", "score": 5}], [], [], [])
        self.assertEqual(result["calibration"]["status"], "unavailable")
        self.assertIsNone(result["calibration"]["ratings"]["exact_agreement"])
        self.assertIsNone(result["calibration"]["pairwise"]["exact_agreement"])
        self.assertIn("not been calibrated", " ".join(result["calibration"]["notes"]))

    def test_duplicate_judgments_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            analyze_judgments([{"key": "x", "score": 1},
                               {"key": "x", "score": 2}], [], [], [])

    def test_reuses_model_judgments_without_new_calls(self):
        report = sample_report()
        template = human_review_template(report)
        baseline1, candidate1 = pair_keys(report, "one")
        template["scores"] = [
            {"key": baseline1, "score": 4},
            {"key": candidate1, "score": 2},
        ]
        template["pairs"] = [
            {"baseline_key": baseline1, "candidate_key": candidate1,
             "preferred": "baseline"}
        ]
        stored = {
            "experiment_id": report["id"], "candidate": "candidate",
            "llm_scores": [
                {"key": baseline1, "score": 4, "source": "model"},
                {"key": candidate1, "score": 3, "source": "model"},
            ],
            "pairwise": [
                {"baseline_key": baseline1, "candidate_key": candidate1,
                 "preferred": "candidate", "source": "model"}
            ],
            "methods": ["llm_judge", "pairwise_judge"],
            "model_judgment_calls": 3,
            "rubric": template["rubric"],
        }
        initial = deepcopy(stored)
        result = calibrate_saved(report, stored, template)
        self.assertEqual(stored, initial)
        self.assertEqual(result["new_model_judgment_calls"], 0)
        self.assertEqual(result["calibration"]["ratings"]["matched"], 2)
        self.assertEqual(result["calibration"]["ratings"]["exact_agreement"], 0.5)
        self.assertEqual(result["calibration"]["pairwise"]["exact_agreement"], 0)
        self.assertEqual(result["matrix"]["human"]["stars"]["count"], 2)
        self.assertEqual(result["model_judgment_calls"], 3)

    def test_refuses_wrong_experiment_or_missing_judge(self):
        report = sample_report()
        human = human_review_template(report)
        human["scores"][0]["score"] = 4
        model = {
            "experiment_id": report["id"], "candidate": "candidate",
            "llm_scores": [], "pairwise": [],
        }
        with self.assertRaisesRegex(ValueError, "no LLM"):
            calibrate_saved(report, model, human)
        model["llm_scores"] = [{"key": human["scores"][0]["key"],
                               "score": 5, "source": "model"}]
        model["experiment_id"] = "other"
        with self.assertRaisesRegex(ValueError, "different experiment"):
            calibrate_saved(report, model, human)
        model["experiment_id"] = report["id"]
        model["llm_scores"][0]["key"] = "fabricated"
        with self.assertRaisesRegex(ValueError, "Invalid"):
            calibrate_saved(report, model, human)
        model["llm_scores"][0]["key"] = human["scores"][0]["key"]
        with self.assertRaisesRegex(ValueError, "Human review is empty"):
            calibrate_saved(report, model, human_review_template(report))

    def test_short_calibration_command_requires_human_file(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "experiment.json"
            run.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "human"):
                expand_quick_command(parser().parse_args(["q", "calibrate", str(run)]))
            args = expand_quick_command(parser().parse_args([
                "q", "calibrate", str(run), "--human", "review.json"
            ]))
            self.assertEqual(args.command, "calibrate")
            self.assertEqual(args.judge_results, run.parent / "qualitative.json")


if __name__ == "__main__":
    unittest.main()
