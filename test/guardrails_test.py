"""Policy tests against the durable, executable task scheduler."""
import tempfile
from pathlib import Path
import unittest

from alpha.worker_runtime import RunStore, load_identities


def request(prompt="child"):
    return {"action": "delegate", "result": "", "children": [
        {"identity": "general", "prompt": prompt,
         "metadata": {"expected_tokens": 20, "file_bytes": 0, "tool_count": 0}}]}


class GuardrailsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "run.sqlite"
        self.base = {"max_depth": 5, "max_children": 5, "max_tasks": 20,
                     "max_rounds": 3, "max_attempts": 2}

    def start(self, **extra):
        limits = {**self.base, **extra}
        store = RunStore(self.db)
        store.start("root", "general", load_identities(None), limits)
        return store

    def test_descendants_limit_checks_entire_ancestor_chain(self):
        store = self.start(max_descendants=2)
        root = store.claim("a", 2, 120, 2)
        reply = request("a")
        reply["children"].append(request("b")["children"][0])
        store.settle(root, reply, store.config())
        child = store.claim("a", 2, 120, 2)
        with self.assertRaisesRegex(ValueError, "Maximum descendants"):
            store.settle(child, request("grandchild"), store.config())
        self.assertEqual(len(store.status()["tasks"]), 3)
        self.assertEqual(store.status()["counts"]["running"], 1)

    def test_dependency_depth_and_cycle_enforced_transactionally(self):
        store = self.start(allow_dependency_links=True, max_dependency_depth=2)
        root = store.claim("a", 2, 120, 2)
        reply = request("first")
        reply["children"].append(request("second")["children"][0])
        store.settle(root, reply, store.config())
        children = [t["id"] for t in store.status()["tasks"] if t["parent_id"] == root["id"]]
        a, b = children
        store.add_dependency(a, b)
        with self.assertRaisesRegex(ValueError, "cycle"):
            store.add_dependency(b, a)
        with self.assertRaisesRegex(ValueError, "cycle"):
            store.add_dependency(a, root["id"])
        conn = store.connect()
        self.assertEqual(conn.execute("SELECT count(*) FROM task_dependencies").fetchone()[0], 1)
        conn.close()

    def test_optional_dependency_depth_rejects_longer_chain(self):
        store = self.start(allow_dependency_links=True, max_dependency_depth=1)
        root = store.claim("a", 2, 120, 2)
        reply = request("first")
        reply["children"].append(request("second")["children"][0])
        store.settle(root, reply, store.config())
        a, b = [t["id"] for t in store.status()["tasks"] if t["parent_id"] == root["id"]]
        with self.assertRaisesRegex(ValueError, "dependency depth"):
            store.add_dependency(a, b)
        conn = store.connect()
        self.assertEqual(conn.execute("SELECT count(*) FROM task_dependencies").fetchone()[0], 0)
        conn.close()

    def test_cross_dependencies_disabled_unless_requested(self):
        store = self.start()
        root = store.claim("a", 2, 120, 2)
        store.settle(root, request(), store.config())
        child = next(t["id"] for t in store.status()["tasks"] if t["parent_id"])
        with self.assertRaisesRegex(ValueError, "disabled"):
            store.add_dependency(child, root["id"])

    def test_call_budget_admission_is_atomic(self):
        store = self.start(max_calls=1)
        root = store.claim("a", 2, 120, 2)
        store.settle(root, request(), store.config())
        self.assertIsNone(store.claim("b", 2, 120, 2))
        status = store.status()
        self.assertEqual(status["budget"]["calls"], 1)
        self.assertEqual(status["counts"]["blocked"], 2)

    def test_token_budget_uses_real_reported_usage(self):
        store = self.start(max_tokens=100)
        root = store.claim("a", 2, 120, 2)
        store.settle(root, request(), store.config(),
                     usage={"input_tokens": 80, "output_tokens": 25}, model="m")
        self.assertEqual(store.status()["budget"]["reported_tokens"], 105)
        self.assertIsNone(store.claim("b", 2, 120, 2))

    def test_unknown_usage_pauses_token_budget(self):
        store = self.start(max_tokens=1000)
        root = store.claim("a", 2, 120, 2)
        store.settle(root, request(), store.config())
        self.assertEqual(store.status()["budget"]["unknown_usage_attempts"], 1)
        self.assertIsNone(store.claim("b", 2, 120, 2))

    def test_cost_budget_rejects_unpriced_models_before_db_creation(self):
        limits = {**self.base, "max_cost_usd": 0.001,
                  "prices": {"m": {"input": 1, "cached_input": 0, "output": 0}}}
        store = RunStore(self.db)
        with self.assertRaisesRegex(ValueError, "priced model IDs"):
            store.start("root", "general", load_identities(None), limits)
        self.assertFalse(self.db.exists())

    def test_cost_budget_stops_new_work_after_observed_spend(self):
        prices = {"m": {"input": 1, "cached_input": 0, "output": 0}}
        limits = {**self.base, "max_cost_usd": 0.001, "prices": prices}
        models = {"small": "m", "medium": "m", "large": "m"}
        store = RunStore(self.db)
        store.start("root", "general", load_identities(None), limits, models=models)
        root = store.claim("a", 2, 120, 2, models=models)
        store.settle(root, request(), store.config(),
                     usage={"input_tokens": 1000, "output_tokens": 0}, model="m")
        self.assertAlmostEqual(store.status()["budget"]["estimated_cost_usd"], 0.001)
        self.assertIsNone(store.claim("b", 2, 120, 2, models=models))


if __name__ == "__main__":
    unittest.main()
