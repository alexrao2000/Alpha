"""Executable subagent orchestration failure-mode and recovery tests."""

import json
from pathlib import Path
import tempfile
import unittest

from alpha.cli import main, parser
from alpha.orchestration import AgentIdentity, Scheduler, Worker, classify, demo


def lab(workers=None, *, max_depth=3, max_children=4, max_attempts=3):
    return Scheduler(
        workers or [Worker("w1", "large"), Worker("w2", "large")],
        [
            AgentIdentity("lead", ("plan", "write"), ("read", "search", "delegate", "write")),
            AgentIdentity("reader", ("research",), ("read", "search")),
        ],
        max_depth=max_depth, max_children=max_children, max_attempts=max_attempts,
    )


class OrchestrationTests(unittest.TestCase):
    def test_cycle_self_and_indirect_are_rejected_without_mutating_graph(self):
        s = lab()
        for name in ("a", "b", "c"):
            s.submit(name, "lead")
        with self.assertRaisesRegex(ValueError, "cycle"):
            s.depends("a", "a")
        s.depends("a", "b")
        s.depends("b", "c")
        with self.assertRaisesRegex(ValueError, "cycle"):
            s.depends("c", "a")
        self.assertEqual(s.tasks["c"].prerequisites, set())
        self.assertFalse(s.dispatch()[0]["task"] == "a")

    def test_all_parents_can_yield_and_children_then_resume(self):
        s = lab([Worker("w1", "small"), Worker("w2", "small")])
        s.submit("p1", "lead")
        s.submit("p2", "lead")
        self.assertEqual(len(s.dispatch()), 2)
        s.delegate("p1", [{"id": "c1", "identity": "reader"}])
        s.delegate("p2", [{"id": "c2", "identity": "reader"}])
        self.assertEqual(s.summary()["running_workers"], [])
        self.assertEqual(len(s.dispatch()), 2)
        s.finish("c1")
        self.assertEqual(s.tasks["p1"].state, "queued")
        self.assertEqual(s.tasks["p2"].state, "waiting")
        s.finish("c2")
        self.assertEqual({row["task"] for row in s.dispatch()}, {"p1", "p2"})
        s.finish("p1")
        s.finish("p2")
        self.assertEqual(s.summary()["states"]["done"], 4)

    def test_two_children_must_both_finish_before_parent_resumes(self):
        s = lab()
        s.submit("p", "lead")
        s.dispatch()
        s.delegate("p", [{"id": "a", "identity": "reader"},
                         {"id": "b", "identity": "reader"}])
        s.dispatch()
        s.finish("a")
        self.assertEqual(s.tasks["p"].state, "waiting")
        s.finish("b")
        self.assertEqual(s.tasks["p"].state, "queued")

    def test_size_routing_uses_metadata_and_preserves_large_slot(self):
        self.assertEqual(classify({"expected_tokens": 12000}), "large")
        self.assertEqual(classify({"expected_tokens": 3500}), "medium")
        self.assertEqual(classify({"expected_tokens": 100}), "small")
        self.assertEqual(classify({"tool_count": 9}), "large")
        self.assertEqual(classify({"file_bytes": 100000}), "medium")
        s = lab([Worker("tiny", "small"), Worker("regular", "medium"),
                 Worker("heavy", "large", ("gpu",))])
        s.submit("small", "lead", {"expected_tokens": 100})
        s.submit("big", "lead", {"expected_tokens": 20000})
        s.submit("medium", "reader", {"expected_tokens": 5000})
        assigned = {row["task"]: row for row in s.dispatch()}
        self.assertEqual(assigned["big"]["worker"], "heavy")
        self.assertEqual(assigned["big"]["model"], "capable")
        self.assertEqual(assigned["medium"]["worker"], "regular")
        self.assertEqual(assigned["medium"]["model"], "balanced")
        self.assertEqual(assigned["small"]["worker"], "tiny")
        self.assertEqual(assigned["small"]["model"], "economy")

    def test_special_requirements_block_incompatible_workers(self):
        s = lab([Worker("no-gpu", "large")])
        s.submit("gpu", "lead", {"requires": ["gpu"], "expected_tokens": 15000})
        self.assertEqual(s.dispatch(), [])
        self.assertEqual(s.tasks["gpu"].state, "queued")
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            classify({"expected_tokens": -1})

    def test_workers_load_skills_and_identity_per_task(self):
        s = lab([Worker("w1", "small"), Worker("w2", "small")])
        s.submit("parent", "lead")
        first = s.dispatch()[0]
        self.assertEqual(first["loaded_skills"], ["plan", "write"])
        s.submit("other", "lead")
        s.dispatch()
        s.delegate("parent", [{"id": "child", "identity": "reader"}])
        slot = s.dispatch()[0]
        self.assertEqual(slot["worker"], "w1")
        self.assertEqual(slot["identity"], "reader")
        self.assertEqual(slot["loaded_skills"], ["research"])
        s.finish("child")
        s.finish("other")
        resumed = s.dispatch()[0]
        self.assertEqual(resumed["identity"], "lead")
        self.assertEqual(resumed["loaded_skills"], ["plan", "write"])

    def test_delegation_caps_permissions_and_atomic_validation(self):
        s = lab(max_depth=1, max_children=1)
        s.submit("root", "lead")
        s.dispatch()
        with self.assertRaisesRegex(ValueError, "fanout"):
            s.delegate("root", [{"id": "one", "identity": "reader"},
                                {"id": "two", "identity": "reader"}])
        self.assertEqual(list(s.tasks), ["root"])
        with self.assertRaisesRegex(ValueError, "Unknown child"):
            s.delegate("root", [{"id": "invalid", "identity": "ghost"}])
        self.assertEqual(s.tasks["root"].state, "running")
        s.delegate("root", [{"id": "child", "identity": "reader"}])
        s.dispatch()
        with self.assertRaisesRegex(PermissionError, "cannot delegate"):
            s.delegate("child", [{"id": "grandchild", "identity": "lead"}])
        self.assertEqual(s.tasks["root"].state, "waiting")
        d = lab(max_depth=1)
        d.submit("root", "lead")
        d.dispatch()
        d.delegate("root", [{"id": "child", "identity": "lead"}])
        d.dispatch()
        with self.assertRaisesRegex(ValueError, "depth"):
            d.delegate("child", [{"id": "grandchild", "identity": "reader"}])

    def test_unknown_tool_is_marked_before_execution_and_blocks_retry(self):
        s = lab()
        s.submit("write", "lead")
        s.dispatch()
        s.tool_call("write", "write", operation_id="external-42", outcome="uncertain")
        s.fail("write")
        self.assertEqual(s.tasks["write"].state, "needs_review")
        self.assertFalse(s.retry("write"))
        events = [e["action"] for e in s.events]
        self.assertLess(events.index("side_effect_flag"), events.index("tool_result"))
        self.assertLess(events.index("side_effect_flag"), events.index("failed"))
        s.resolve("write", completed=False)
        self.assertEqual(s.tasks["write"].state, "failed")

    def test_unknown_tools_require_operation_id_and_restrict_access(self):
        s = lab()
        s.submit("task", "reader")
        s.dispatch()
        with self.assertRaises(PermissionError):
            s.tool_call("task", "write", operation_id="x")
        self.assertFalse(s.tasks["task"].side_effect_possible)
        self.assertEqual(s.tool_call("task", "read"), "ok")
        s.finish("task")
        t = lab()
        t.submit("task", "lead")
        t.dispatch()
        with self.assertRaisesRegex(ValueError, "operation_id"):
            t.tool_call("task", "write")

    def test_idempotency_receipt_prevents_duplicate_side_effect(self):
        s = lab()
        s.submit("a", "lead")
        s.dispatch()
        self.assertEqual(s.tool_call("a", "write", operation_id="unique-1"), "ok")
        s.finish("a")
        s.submit("b", "lead")
        s.dispatch()
        self.assertEqual(s.tool_call("b", "write", operation_id="unique-1"), "deduplicated")
        self.assertFalse(s.tasks["b"].side_effect_possible)
        self.assertEqual(len(s.effect_ledger), 1)
        with self.assertRaisesRegex(ValueError, "different operation"):
            s.tool_call("b", "read", operation_id="unique-1")

    def test_uncertain_receipt_requires_manual_reconciliation(self):
        s = lab()
        s.submit("a", "lead")
        s.dispatch()
        s.tool_call("a", "write", operation_id="uncertain-1", outcome="uncertain")
        s.fail("a")
        s.submit("b", "lead")
        s.dispatch()
        self.assertEqual(s.tool_call("b", "write", operation_id="uncertain-1"), "needs_review")
        s.fail("b")
        self.assertEqual(s.tasks["b"].state, "needs_review")

    def test_safe_retry_is_bounded(self):
        s = lab(max_attempts=2)
        s.submit("a", "lead")
        s.dispatch()
        s.fail("a")
        self.assertTrue(s.retry("a"))
        s.dispatch()
        s.fail("a")
        self.assertFalse(s.retry("a"))
        self.assertEqual(s.tasks["a"].state, "failed")

    def test_restore_requeues_safe_inflight_and_retains_waiting_parent(self):
        s = lab()
        s.submit("parent", "lead")
        s.dispatch()
        s.delegate("parent", [{"id": "child", "identity": "reader"}])
        s.dispatch()
        saved = json.loads(json.dumps(s.snapshot()))
        recovered = Scheduler.restore(saved)
        self.assertEqual(recovered.tasks["parent"].state, "waiting")
        self.assertEqual(recovered.tasks["child"].state, "queued")
        self.assertEqual(recovered.summary()["running_workers"], [])
        self.assertEqual(recovered.dispatch()[0]["identity"], "reader")
        recovered.finish("child")
        self.assertEqual(recovered.dispatch()[0]["identity"], "lead")

    def test_safe_child_retry_reopens_blocked_parent(self):
        s = lab()
        s.submit("parent", "lead")
        s.dispatch()
        s.delegate("parent", [{"id": "child", "identity": "reader"}])
        s.dispatch()
        s.fail("child")
        self.assertEqual(s.tasks["parent"].state, "blocked")
        self.assertTrue(s.retry("child"))
        self.assertEqual(s.tasks["parent"].state, "waiting")
        s.dispatch()
        s.finish("child")
        self.assertEqual(s.tasks["parent"].state, "queued")

    def test_reconciliation_can_resume_previously_blocked_parent(self):
        s = lab()
        s.submit("parent", "lead")
        s.dispatch()
        s.delegate("parent", [{"id": "child", "identity": "lead"}])
        s.dispatch()
        s.tool_call("child", "write", operation_id="external-1", outcome="uncertain")
        s.fail("child")
        self.assertEqual(s.tasks["parent"].state, "blocked")
        s.resolve("child", completed=True)
        self.assertEqual(s.tasks["parent"].state, "queued")
        s.dispatch()
        s.finish("parent")
        self.assertEqual(s.tasks["parent"].state, "done")

    def test_restore_refuses_uncertain_side_effect_replay(self):
        s = lab()
        s.submit("parent", "lead")
        s.dispatch()
        s.delegate("parent", [{"id": "child", "identity": "lead"}])
        s.dispatch()
        s.tool_call("child", "write", operation_id="persist-1")
        recovered = Scheduler.restore(json.loads(json.dumps(s.snapshot())))
        self.assertEqual(recovered.tasks["child"].state, "needs_review")
        self.assertEqual(recovered.tasks["parent"].state, "blocked")
        self.assertFalse(recovered.retry("child"))

    def test_transitive_failure_and_deadline_prevent_infinite_wait(self):
        s = lab()
        for name in ("grandparent", "parent", "child"):
            s.submit(name, "lead")
        s.depends("grandparent", "parent")
        s.depends("parent", "child")
        s.expire("child")
        self.assertEqual(s.tasks["parent"].state, "blocked")
        self.assertEqual(s.tasks["grandparent"].state, "blocked")
        self.assertFalse(s.dispatch())

    def test_demo_reports_all_scenarios(self):
        result = demo("all")
        self.assertTrue(result["not_a_live_agent_benchmark"])
        self.assertEqual(len(result["results"]), 6)
        self.assertTrue(result["results"]["cycles"]["cycle_rejected"])
        self.assertTrue(result["results"]["starvation"]["slots_released_while_waiting"])
        self.assertTrue(result["results"]["side-effects"]["blind_retry_refused"])
        self.assertTrue(result["results"]["limits"]["depth_rejected"])

    def test_cli_writes_json_without_model_calls(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "report.json"
            args = parser().parse_args(["orch", "demo", "--scenario", "cycles", "--out", str(out)])
            self.assertEqual(args.scenario, "cycles")
            self.assertEqual(main(["orch", "demo", "--scenario", "cycles", "--out", str(out)]), 0)
            self.assertTrue(json.loads(out.read_text())["results"]["cycles"]["cycle_rejected"])


if __name__ == "__main__":
    unittest.main()
