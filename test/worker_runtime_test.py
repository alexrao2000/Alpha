"""Integration tests for the persistent executor; fake backend replaces model cost."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from alpha.cli import main, parser
from alpha.worker_runtime import RunStore, load_identities, run_workers


class FakeModel:
    calls = []
    async def complete(self, *, schema, prompt):
        self.calls.append(prompt)
        if "TASK\nmain task" in prompt and "COMPLETED CHILD RESULTS\n[]" in prompt:
            return {"output": {
                "action": "delegate", "result": "",
                "children": [
                    {"identity": "general", "prompt": "child first", "metadata": {"expected_tokens": 100, "tool_count": 0, "file_bytes": 0}},
                    {"identity": "general", "prompt": "child second", "metadata": {"expected_tokens": 100, "tool_count": 0, "file_bytes": 0}},
                ]}}
        if "TASK\nchild" in prompt:
            await asyncio.sleep(.01)
            return {"output": {"action": "complete", "result": "child result", "children": []}}
        return {"output": {"action": "complete", "result": "combined result", "children": []}}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Path(self.folder.name) / "job.sqlite"
        self.identities = load_identities(None)
        self.limits = dict(max_depth=3, max_children=4, max_tasks=12,
                           max_rounds=2, max_attempts=2)
        FakeModel.calls = []

    def store(self):
        store = RunStore(self.db)
        store.start("main task", "general", self.identities, self.limits)
        return store

    def test_real_execution_path_delegates_yields_and_resumes(self):
        store = self.store()
        result = asyncio.run(run_workers(store, workers=2,
            backend_factory=lambda model: FakeModel()))
        self.assertEqual(result["counts"]["done"], 3)
        root = next(t for t in result["tasks"] if t["id"] == result["root"])
        self.assertEqual(json.loads(root["result"]), "combined result")
        self.assertEqual(len(FakeModel.calls), 4)
        self.assertEqual(sum("TASK\nmain task" in p for p in FakeModel.calls), 2)

    def test_waiting_parent_does_not_hold_lease(self):
        store = self.store()
        a = store.claim("worker-1", 2, 120, 2)
        reply = {"action": "delegate", "result": "", "children": [
            {"identity": "general", "prompt": "a", "metadata": {"expected_tokens": 1, "file_bytes": 0, "tool_count": 0}}
        ]}
        self.assertTrue(store.settle(a, reply, store.config()))
        self.assertEqual(store.status()["counts"]["waiting"], 1)
        child = store.claim("worker-1", 2, 120, 2)
        self.assertNotEqual(child["id"], a["id"])
        self.assertEqual(store.status()["counts"]["running"], 1)
        self.assertTrue(store.settle(child, {"action": "complete", "result": "ok", "children": []}, store.config()))
        self.assertEqual(store.status()["counts"]["queued"], 1)
        resumed = store.claim("worker-1", 2, 120, 2)
        self.assertEqual(resumed["id"], a["id"])
        self.assertEqual(resumed["children"][0]["result"], '"ok"')

    def test_fences_expired_lease_and_recovers_in_another_store(self):
        store = self.store()
        first = store.claim("dead-worker", 2, 120, 2)
        conn = store.connect()
        conn.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (first["id"],))
        conn.close()
        recovered = RunStore(self.db).claim("survivor", 2, 120, 2)
        self.assertEqual(recovered["id"], first["id"])
        self.assertFalse(store.settle(first, {"action": "complete", "result": "stale", "children": []}, store.config()))
        self.assertTrue(store.settle(recovered, {"action": "complete", "result": "fresh", "children": []}, store.config()))
        self.assertEqual(store.status()["counts"]["done"], 1)

    def test_two_claims_cannot_take_same_task(self):
        store = self.store()
        first = store.claim("one", 2, 120, 2)
        second = RunStore(self.db).claim("two", 2, 120, 2)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_child_failure_blocks_parent(self):
        store = self.store()
        parent = store.claim("w1", 2, 120, 2)
        request = {"action": "delegate", "result": "", "children": [
            {"identity": "general", "prompt": "a", "metadata": {"expected_tokens": 1, "file_bytes": 0, "tool_count": 0}}]}
        store.settle(parent, request, store.config())
        child = store.claim("w2", 2, 120, 2)
        self.assertTrue(store.error(child, "failure", 1))
        self.assertEqual(store.status()["counts"]["blocked"], 1)
        self.assertTrue(store.finished())

    def test_invalid_delegation_is_atomic(self):
        store = self.store()
        parent = store.claim("w1", 2, 120, 2)
        with self.assertRaisesRegex(ValueError, "Unknown identity"):
            store.settle(parent, {"action": "delegate", "result": "",
                "children": [{"identity": "missing", "prompt": "foo",
                              "metadata": {"expected_tokens": 1}}]}, store.config())
        self.assertEqual(len(store.status()["tasks"]), 1)
        self.assertEqual(store.status()["counts"]["running"], 1)

    def test_resume_calls_real_worker_path_not_demo(self):
        store = self.store()
        result = asyncio.run(run_workers(RunStore(self.db), workers=1,
            backend_factory=lambda model: FakeModel()))
        self.assertEqual(result["counts"]["done"], 3)

    def test_identities_load_skill_files(self):
        folder = Path(self.folder.name)
        (folder / "expert.md").write_text("Use careful analysis.")
        path = folder / "agents.json"
        path.write_text(json.dumps({"identities": {
            "expert": {"instructions": "Review.", "skills": ["expert.md"], "can_delegate": False}}}))
        identities = load_identities(path)
        self.assertEqual(identities["expert"]["skills"][0]["instructions"], "Use careful analysis.")

    def test_cli_status_does_not_invoke_provider(self):
        self.store()
        args = parser().parse_args(["orch", "run", "hi", "--workers", "3"])
        self.assertEqual(args.task, "hi")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["orch", "status", "--db", str(self.db)]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "queued")


if __name__ == "__main__":
    unittest.main()
