"""Regression tests for the optional, non-authoritative evaluation layer."""

import asyncio
from copy import deepcopy
import unittest

from alpha.qualitative import (
    evaluate, human_review_template, paired_holdout, run_key,
    simulated_interactions,
)


def make_run(variant, passed, case="case-1", trial=0):
    return {
        "case_id": case, "category": "test", "split": "holdout", "variant": variant,
        "trial": trial,
        "trace": [{"role": "writer", "context": {"request": "Write a helpful response"},
                   "output": {"text": "hello" if passed else "hmm"}, "model_calls": 1}],
        "final_state": {"artifact": "a response" if passed else "another response"},
        "verdict": {"passed": passed, "unsafe": False, "checks": []},
        "tools": [], "elapsed_ms": 12, "error": None,
    }


def make_report():
    return {
        "id": "experiment-1", "selected": "candidate", "validated": True,
        "variants": [{"id": "baseline"}, {"id": "candidate"}],
        "runs": [make_run("baseline", True), make_run("candidate", False)],
    }


class JudgeStub:
    def __init__(self):
        self.scored = []
        self.compared = []

    async def score(self, run):
        self.scored.append(run_key(run))
        return {"score": 3, "reason": "Acceptable"}

    async def compare(self, a, b):
        self.compared.append((a["variant"], b["variant"]))
        return {"winner": "tie", "reason": "No clear preference"}


class QualitativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_by_default_and_no_report_mutation(self):
        report = make_report()
        prior = deepcopy(report)
        result = await evaluate(report)
        self.assertEqual(result["methods"], [])
        self.assertEqual(report, prior)

    async def test_turns_and_explicit_corrections(self):
        report = make_report()
        rows = {"experiment_id": report["id"], "interactions": [{
            "split": "holdout", "case_id": "case-1", "trial": 0, "variant": "baseline",
            "messages": [
                {"role": "user", "content": "Question"},
                {"role": "assistant", "content": "Answer"},
                {"role": "user", "content": "Can you expand?", "correction": False},
                {"role": "assistant", "content": "Explanation"},
                {"role": "user", "content": "That's incorrect", "correction": True},
            ],
            "satisfaction": 3, "abandoned": False,
        }]}
        result = await evaluate(report, interactions=rows, turn_count=True, feedback=True)
        self.assertEqual(result["interactions"][0]["user_turns"], 3)
        self.assertEqual(result["interactions"][0]["follow_up_turns"], 2)
        self.assertEqual(result["interactions"][0]["correction_turns"], 1)
        self.assertEqual(result["interaction_summary"]["follow_up_rate"], 1)
        self.assertEqual(result["interaction_summary"]["correction_rate_per_follow_up"], 0.5)

    async def test_simulated_feedback_is_marked_tautological(self):
        report = make_report()
        result = await evaluate(report, simulate_feedback=True)
        self.assertEqual(result["interaction_summary"]["count"], 2)
        self.assertIn("Synthetic feedback", " ".join(result["notes"]))
        self.assertIn("synthetic_from_objective_verdict",
                      [r["source"] for r in result["interactions"]])
        self.assertEqual(result["interaction_summary"]["follow_up_rate"], 0.5)

    async def test_bad_interactions_fail_closed(self):
        report = make_report()
        bad = simulated_interactions(report)
        bad["interactions"][0]["case_id"] = "unknown"
        with self.assertRaises(ValueError):
            await evaluate(report, interactions=bad, turn_count=True)
        with self.assertRaises(ValueError):
            await evaluate(report, interactions={}, simulate_feedback=True)

    async def test_human_reviews_are_optional_and_validate_inputs(self):
        report = make_report()
        template = human_review_template(report)
        self.assertEqual(len(template["scores"]), 2)
        self.assertEqual(len(template["pairs"]), 1)
        template["scores"][0]["score"] = 4
        template["pairs"][0]["preferred"] = "candidate"
        result = await evaluate(report, human=template)
        self.assertEqual(result["human"]["scores"][0]["score"], 4)
        self.assertEqual(result["human"]["pairs"][0]["preferred"], "candidate")
        template["scores"][0]["score"] = 9
        with self.assertRaises(ValueError):
            await evaluate(report, human=template)

    async def test_judge_budget_and_pairs(self):
        report = make_report()
        stub = JudgeStub()
        result = await evaluate(report, judge=stub, score=True, pairwise=True,
                                max_judgments=3)
        self.assertEqual(result["model_judgment_calls"], 3)
        self.assertEqual(len(result["llm_scores"]), 2)
        self.assertEqual(result["pairwise_preferences"]["tie"], 1)
        self.assertEqual(len(stub.compared), 1)
        self.assertEqual(report["selected"], "candidate")

    async def test_large_holdout_leaves_budget_for_pairwise(self):
        report = make_report()
        report["runs"] = [
            make_run(variant, True, case=f"case-{i}")
            for i in range(10) for variant in ("baseline", "candidate")
        ]
        judge = JudgeStub()
        result = await evaluate(report, judge=judge, score=True, pairwise=True,
                                max_judgments=6)
        self.assertEqual(len(judge.scored), 3)
        self.assertEqual(len(judge.compared), 3)
        self.assertEqual(result["model_judgment_calls"], 6)

    async def test_judge_cap_does_not_add_hidden_calls(self):
        judge = JudgeStub()
        result = await evaluate(make_report(), judge=judge,
                                score=True, pairwise=True, max_judgments=1)
        self.assertEqual(len(judge.scored), 1)
        self.assertEqual(len(judge.compared), 0)
        self.assertEqual(result["model_judgment_calls"], 1)


if __name__ == "__main__":
    unittest.main()
