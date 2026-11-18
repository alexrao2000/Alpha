"""Optional Docker isolation for external agent commands.

Docker uses a trusted daemon; containers are not a complete hostile-code boundary.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import shlex
from pathlib import Path

from .processes import run_process

_IMAGE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./:-]*@sha256:[a-fA-F0-9]{64}$")
_RUNTIME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,63}$")
_FIELDS = {
    "mode", "image", "network", "memory_mb", "cpus", "pids_limit",
    "timeout_seconds", "tmpfs_mb", "max_output_kb", "workspace", "workspace_write", "runtime",
}


def validate_sandbox_policy(policy: dict | None, *, has_command: bool) -> dict | None:
    if policy is None:
        return None
    if not has_command:
        raise ValueError("A sandbox policy requires an external --command.")
    if not isinstance(policy, dict) or policy.keys() - _FIELDS:
        raise ValueError("Sandbox policy must contain only recognized fields.")
    if policy.get("mode") != "docker":
        raise ValueError("Sandbox mode must be 'docker'.")
    image = policy.get("image")
    if not isinstance(image, str) or not _IMAGE.fullmatch(image):
        raise ValueError("Sandbox image must be pinned to a sha256 digest.")
    network = policy.get("network", "none")
    if network not in ("none", "bridge"):
        raise ValueError("Sandbox network must be 'none' or 'bridge'.")
    result = {"mode": "docker", "image": image, "network": network}
    for name, default, low, high in (
        ("memory_mb", 512, 64, 131072),
        ("pids_limit", 128, 1, 4096),
        ("timeout_seconds", 300, 1, 86400),
        ("tmpfs_mb", 64, 16, 1024),
        ("max_output_kb", 1024, 16, 65536),
    ):
        value = policy.get(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Sandbox {name} must be an integer between {low} and {high}.")
        result[name] = value
    cpus = policy.get("cpus", 1.0)
    if type(cpus) not in (int, float) or not math.isfinite(cpus) or not 0.1 <= cpus <= 64:
        raise ValueError("Sandbox cpus must be a finite number between 0.1 and 64.")
    result["cpus"] = float(cpus)
    workspace = policy.get("workspace")
    if workspace is not None:
        if not isinstance(workspace, str) or not Path(workspace).is_absolute():
            raise ValueError("Sandbox workspace must be an absolute directory.")
        resolved = Path(workspace).resolve(strict=True)
        if not resolved.is_dir() or resolved == Path("/") or "," in str(resolved):
            raise ValueError("Sandbox workspace must be an existing safe directory.")
        result["workspace"] = str(resolved)
    else:
        result["workspace"] = None
    write = policy.get("workspace_write", False)
    if type(write) is not bool or (write and workspace is None):
        raise ValueError("workspace_write must be boolean and requires workspace.")
    result["workspace_write"] = write
    runtime = policy.get("runtime")
    if runtime is not None and (not isinstance(runtime, str) or not _RUNTIME.fullmatch(runtime)):
        raise ValueError("Sandbox runtime must be a configured Docker runtime name.")
    result["runtime"] = runtime
    return result


def docker_command(command: str, policy: dict, *, container_name: str) -> list[str]:
    argv = shlex.split(command)
    if not argv:
        raise ValueError("Agent command cannot be empty.")
    memory = policy["memory_mb"] * 1024 * 1024
    args = [
        "docker", "run", "--rm", "--interactive", "--init", "--pull", "never",
        "--name", container_name, "--label", "alpha.sandbox=agent-command",
        "--network", policy["network"], "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--user", "65534:65534",
        "--env", "HOME=/tmp", "--pids-limit", str(policy["pids_limit"]),
        "--memory", str(memory), "--memory-swap", str(memory),
        "--cpus", str(policy["cpus"]),
        "--tmpfs", f"/tmp:rw,nosuid,nodev,size={policy['tmpfs_mb'] * 1024 * 1024}",
        "--stop-timeout", "3",
    ]
    if policy["runtime"]:
        args.extend(["--runtime", policy["runtime"]])
    if policy["workspace"]:
        mount = f"type=bind,source={policy['workspace']},target=/workspace"
        if not policy["workspace_write"]:
            mount += ",readonly"
        args.extend(["--mount", mount, "--workdir", "/workspace"])
    else:
        args.extend(["--workdir", "/tmp"])
    return [*args, policy["image"], *argv]


async def _remove_container(name: str) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", "rm", "--force", name,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()
    except OSError:
        pass


async def run_agent_command(command: str, payload: str, policy: dict | None, *,
                            task_id: str, lease_token: str,
                            signal: asyncio.Event | None = None) -> str:
    if policy is None:
        return await run_process(shlex.split(command), payload, timeout=300,
                                 signal=signal, label="Agent command")
    policy = validate_sandbox_policy(policy, has_command=True)
    digest = hashlib.sha256(f"{task_id}:{lease_token}".encode()).hexdigest()[:24]
    name = f"alpha-agent-{digest}"
    try:
        return await run_process(docker_command(command, policy, container_name=name),
                                 payload, timeout=policy["timeout_seconds"],
                                 signal=signal, label="Sandboxed agent command",
                                 max_output_bytes=policy["max_output_kb"] * 1024)
    finally:
        await _remove_container(name)
