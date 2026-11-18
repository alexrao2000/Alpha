"""Sandbox policy, execution path and bounded process output."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from alpha.cli import parser
from alpha.processes import run_process
from alpha.sandbox import docker_command, run_agent_command, validate_sandbox_policy
from alpha.worker_runtime import RunStore, load_identities, run_workers

IMAGE = "registry.example.invalid/alpha-agent@sha256:" + "a" * 64


class PolicyTests(unittest.TestCase):
    def test_defaults_are_restrictive(self):
        policy = validate_sandbox_policy({"mode": "docker", "image": IMAGE}, has_command=True)
        argv = docker_command("python -m agent", policy, container_name="task-test")
        self.assertEqual(argv[:2], ["docker", "run"])
        self.assertIn("--read-only", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("--user") + 1], "65534:65534")
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertNotIn("--mount", argv)

    def test_invalid_policy_rejected(self):
        cases = [
            {"mode": "docker", "image": "latest"},
            {"mode": "docker", "image": IMAGE, "privileged": True},
            {"mode": "docker", "image": IMAGE, "memory_mb": 0},
            {"mode": "docker", "image": IMAGE, "workspace_write": True},
            {"mode": "docker", "image": IMAGE, "network": "host"},
        ]
        for policy in cases:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_sandbox_policy(policy, has_command=True)
        with self.assertRaises(ValueError):
            validate_sandbox_policy({"mode": "docker", "image": IMAGE}, has_command=False)

    def test_workspace_is_read_only_unless_opted_in(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = validate_sandbox_policy(
                {"mode": "docker", "image": IMAGE, "workspace": folder},
                has_command=True)
            argv = docker_command("python agent.py", policy, container_name="task")
            self.assertIn(",readonly", argv[argv.index("--mount") + 1])
            policy["workspace_write"] = True
            argv = docker_command("python agent.py", policy, container_name="task")
            self.assertNotIn("readonly", argv[argv.index("--mount") + 1])

    def test_cli_exposes_policy(self):
        args = parser().parse_args(["orch", "run", "do work", "--command", "agent",
                                    "--sandbox-policy", "sandbox.json"])
        self.assertEqual(args.sandbox_policy, Path("sandbox.json"))


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_docker_dispatch_and_cleanup(self):
        policy = validate_sandbox_policy({"mode": "docker", "image": IMAGE}, has_command=True)
        with patch("alpha.sandbox.run_process", new_callable=AsyncMock,
                   return_value='{"action":"complete"}') as proc, \
             patch("alpha.sandbox._remove_container", new_callable=AsyncMock) as cleanup:
            self.assertEqual(await run_agent_command(
                "python -m agent", "{}", policy, task_id="job", lease_token="lease"),
                '{"action":"complete"}')
        cleanup.assert_awaited_once()
        self.assertEqual(proc.await_args.args[0][:2], ["docker", "run"])
        self.assertEqual(proc.await_args.kwargs["max_output_bytes"], 1024 * 1024)

    async def test_cleanup_after_timeout(self):
        policy = validate_sandbox_policy({"mode": "docker", "image": IMAGE}, has_command=True)
        with patch("alpha.sandbox.run_process", new_callable=AsyncMock,
                   side_effect=TimeoutError), \
             patch("alpha.sandbox._remove_container", new_callable=AsyncMock) as cleanup:
            with self.assertRaises(TimeoutError):
                await run_agent_command("agent", "{}", policy,
                                        task_id="job", lease_token="lease")
        cleanup.assert_awaited_once()

    async def test_output_cap(self):
        result = await run_process([sys.executable, "-c", "print('ok')"], "",
                                   timeout=5, max_output_bytes=16)
        self.assertEqual(result, "ok\n")
        with self.assertRaisesRegex(RuntimeError, "stdout exceeds"):
            await run_process([sys.executable, "-c", "print('x' * 250000)"], "",
                              timeout=5, max_output_bytes=128)

    async def test_bad_output_limit(self):
        with self.assertRaises(ValueError):
            await run_process([sys.executable, "-c", "print('ok')"], "",
                              timeout=5, max_output_bytes=0)


class RuntimeIntegrationTests(unittest.TestCase):
    def test_sandbox_persisted_and_used_by_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RunStore(Path(folder) / "run.sqlite")
            limits = {"max_depth": 3, "max_children": 4, "max_tasks": 12,
                      "max_rounds": 2, "max_attempts": 2}
            store.start("task", "general", load_identities(None), limits,
                        command="python -m agent", read_only_command=True,
                        sandbox_policy={"mode": "docker", "image": IMAGE})
            self.assertEqual(store.config()["sandbox"]["image"], IMAGE)
            response = json.dumps({"action": "complete", "result": "ok", "children": []})
            with patch("alpha.worker_runtime.shutil.which", return_value="/usr/bin/docker"), \
                 patch("alpha.sandbox.run_process", new_callable=AsyncMock,
                       return_value=response), \
                 patch("alpha.sandbox._remove_container", new_callable=AsyncMock):
                result = asyncio.run(run_workers(store, workers=1))
            self.assertEqual(result["counts"]["done"], 1)

    def test_missing_docker_does_not_claim_work(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RunStore(Path(folder) / "run.sqlite")
            limits = {"max_depth": 3, "max_children": 4, "max_tasks": 12,
                      "max_rounds": 2, "max_attempts": 2}
            store.start("task", "general", load_identities(None), limits,
                        command="python -m agent",
                        sandbox_policy={"mode": "docker", "image": IMAGE})
            with patch("alpha.worker_runtime.shutil.which", return_value=None):
                with self.assertRaisesRegex(ValueError, "Docker CLI"):
                    asyncio.run(run_workers(store, workers=1))
            self.assertEqual(store.status()["counts"]["queued"], 1)


if __name__ == "__main__":
    unittest.main()
