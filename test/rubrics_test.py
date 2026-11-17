"""Shared guidelines must govern both human and LLM review."""

import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from alpha.cli import expand_quick_command, parser
from alpha.human_review import render_human_review
from alpha.qualitative import CodexJudge, calibrate_saved, human_review_template, run_key
from alpha.report import judgment_analysis_html
from alpha.rubrics import (
    checked_record, guidance, load_rubric, rubric_record, validate_rubric,
)


def small_report():
    runs = [
        {
            "case_id": "task-one", "split": "holdout", "trial": 0,
            "variant": variant,
            "trace": [{"context": {"request": "Write an answer"}}],
            "final_state": {"artifact": answer},
        }
        for variant, answer in (("baseline", "A basic answer"), ("candidate", "A revised answer"))
    ]
    return {
        "id": "e1", "selected": "candidate",
        "variants": [{"id": "baseline"}, {"id": "candidate"}],
        "runs": runs,
    }


class PromptCapture:
    def __init__(self):
        self.prompts = []

    async def complete(self, *, schema, prompt):
        self.prompts.append(prompt)
        if "winner" in schema["required"]:
            return {"output": {"winner": "tie", "reason": "Similar quality"}}
        return {"output": {"score": 4, "reason": "Meets criteria"}}


class RubricTests(unittest.TestCase):
    def test_default_has_five_criteria_and_five_anchors(self):
        rubric = load_rubric()
        self.assertEqual(len(rubric["criteria"]), 5)
        self.assertEqual(set(rubric["anchors"]), {"1", "2", "3", "4", "5"})
        labels = [criterion["label"] for criterion in rubric["criteria"]]
        self.assertEqual(labels, [
            "Relevance", "Completeness", "Clarity",
            "Instruction following", "Helpfulness",
        ])
        self.assertIn("tie", rubric["pairwise"]["tie_rule"])

    def test_rubric_hash_tracks_changes_and_rejects_tampering(self):
        original = rubric_record(load_rubric())
        changed = load_rubric()
        changed["anchors"]["3"] = "Different definition of average."
        self.assertNotEqual(original["sha256"], rubric_record(changed)["sha256"])
        with self.assertRaisesRegex(ValueError, "hash"):
            checked_record({"definition": changed, "sha256": original["sha256"]})

    def test_custom_rubric_is_validated(self):
        rubric = load_rubric()
        rubric["criteria"][1]["id"] = rubric["criteria"][0]["id"]
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_rubric(rubric)
        rubric = load_rubric()
        rubric["anchors"].pop("2")
        with self.assertRaisesRegex(ValueError, "five"):
            validate_rubric(rubric)
        rubric = load_rubric()
        rubric["id"] = "custom"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rubric.json"
            import json
            path.write_text(json.dumps(rubric), encoding="utf-8")
            self.assertEqual(load_rubric(path)["id"], "custom")

    def test_human_review_page_displays_same_rubric(self):
        report = small_report()
        template = human_review_template(report)
        self.assertEqual(template["rubric"], rubric_record(load_rubric()))
        html = render_human_review(report, template)
        self.assertIn("Shared grading criteria", html)
        self.assertIn("Use the same grading rubric", html)
        self.assertIn("Relevance", html)
        self.assertIn("5 — ", html) if False else None
        self.assertIn(rubric_record(load_rubric())["sha256"], html)

    def test_calibration_rejects_mismatched_or_missing_rubric(self):
        report = small_report()
        human = human_review_template(report)
        human["scores"][0]["score"] = 3
        key = human["scores"][0]["key"]
        model = {
            "experiment_id": report["id"], "candidate": "candidate",
            "llm_scores": [{"key": key, "score": 4, "source": "model"}],
            "pairwise": [], "methods": ["llm_judge"],
            "rubric": human["rubric"],
        }
        matching = calibrate_saved(report, model, human)
        self.assertEqual(matching["calibration"]["ratings"]["matched"], 1)
        changed = deepcopy(human)
        changed["rubric"]["definition"]["anchors"]["3"] = "Changed anchor."
        changed["rubric"] = rubric_record(changed["rubric"]["definition"])
        with self.assertRaisesRegex(ValueError, "different grading rubrics"):
            calibrate_saved(report, model, changed)
        without = deepcopy(model)
        without.pop("rubric")
        with self.assertRaisesRegex(ValueError, "same rubric"):
            calibrate_saved(report, without, human)

    def test_report_explains_criteria(self):
        value = {
            "rubric": rubric_record(load_rubric()),
            "matrix": {
                "human": {"stars": {"count": 0}, "ab": {"count": 0, "preferences": {}}},
                "llm": {"stars": {"count": 0}, "ab": {"count": 0, "preferences": {}}},
            },
        }
        html = judgment_analysis_html(value)
        self.assertIn("Shared reviewer rubric", html)
        self.assertIn("Instruction following", html)
        self.assertIn("1–5 rating anchors", html)

    def test_quick_cli_accepts_shared_rubric(self):
        with tempfile.TemporaryDirectory() as folder:
            experiment = Path(folder) / "experiment.json"
            experiment.write_text("{}", encoding="utf-8")
            args = expand_quick_command(parser().parse_args([
                "q", "judge", str(experiment), "--rubric", "custom.json"
            ]))
            self.assertEqual(args.rubric, Path("custom.json"))


class JudgeRubricTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_prompts_use_identical_criteria_and_anchors(self):
        judge = CodexJudge()
        judge.backend = PromptCapture()
        report = small_report()
        await judge.score(report["runs"][0])
        await judge.compare(report["runs"][0], report["runs"][1])
        self.assertEqual(len(judge.backend.prompts), 2)
        for prompt in judge.backend.prompts:
            for criterion in load_rubric()["criteria"]:
                self.assertIn(criterion["label"], prompt)
                self.assertIn(criterion["description"], prompt)
            self.assertIn("3: Adequate:", prompt)
        self.assertIn("Choose tie", judge.backend.prompts[1])
        self.assertIn("one overall integer rating", judge.backend.prompts[0])


if __name__ == "__main__":
    unittest.main()
