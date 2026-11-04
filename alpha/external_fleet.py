# Verify pinned upstream bytes before every external run. Native worker inputs
# exclude grading oracles; generated artifacts and private checks meet only in
# the separate grader. Framework state and dependencies remain process-isolated.

from __future__ import annotations

import asyncio
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from typing import cast

from .contracts import JSON, ROOT, Run, Verdict, compact_json
from .processes import run_process

HOME = ROOT / "integrations/crewai-game-builder"


def load_external_fleet() -> JSON:
    provenance = json.loads((HOME / "provenance.json").read_text(encoding="utf-8"))
    for file in provenance["files"]:
        data = (HOME / "upstream" / file["path"]).read_bytes()
        digest = sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if digest != file["git_blob_sha"]:
            raise ValueError(f"Upstream source changed: {file['path']}")
    original = ["senior_engineer_agent", "qa_engineer_agent", "chief_qa_engineer_agent"]
    roles = json.loads((HOME / "fleet.config.json").read_text(encoding="utf-8"))
    if (not isinstance(roles, list) or original[0] not in roles
            or any(not isinstance(r, str) or r not in original for r in roles)
            or len(set(roles)) != len(roles) or roles != sorted(roles, key=original.index)):
        raise ValueError("Invalid external fleet configuration. Keep the senior engineer and preserve upstream order.")
    variants = [{"id": "baseline", "label": "Upstream full crew" if len(roles) == 3 else "Configured crew", "roles": roles}]
    variants.extend({"id": f"without-{role}", "label": "Remove QA engineer" if role == "qa_engineer_agent" else "Remove chief QA",
                     "roles": [r for r in roles if r != role]} for role in roles[1:])
    paths = ["src/game_builder_crew/config/agents.yaml", "src/game_builder_crew/config/tasks.yaml", "src/game_builder_crew/crew.py"]
    adapter = sha256()
    for path in [HOME / "worker.py", HOME / "grade.py", *(Path(__file__).resolve().parent / name for name in
                 ("external_fleet.py", "model_bridge.py", "backends.py", "processes.py"))]:
        adapter.update(path.read_bytes())
    return {"cases": json.loads((HOME / "cases.json").read_text(encoding="utf-8")), "variants": variants,
            "prompts": {p: (HOME / "upstream" / p).read_text(encoding="utf-8") for p in paths},
            "fleet": {"id": "crewai-game-builder", "name": "CrewAI Game Builder", "provenance": provenance,
                      "description": "Pinned upstream three-agent software-building crew, executed by its native CrewAI runtime.",
                      "dataset_description": "Alpha-authored headless game programming tasks, with different games in discovery and holdout.",
                      "config_path": "integrations/crewai-game-builder/fleet.config.json", "adapter_sha256": adapter.hexdigest()}}


async def run_python(python: str, script: Path, data: JSON, signal: asyncio.Event | None, timeout: float) -> JSON:
    env = {**os.environ, "OTEL_SDK_DISABLED": "true", "CREWAI_TELEMETRY_ENABLED": "false", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    stdout = await run_process([python, str(script)], compact_json(data), timeout=timeout, signal=signal,
                               env=env, label="External runtime")
    try:
        value = json.loads(stdout)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError:
        raise RuntimeError("External runtime returned invalid JSON.") from None


async def run_external_case(*, test_case: JSON, variant: JSON, backend, trial: int,
                            signal: asyncio.Event | None = None, **_) -> Run:
    load_external_fleet()
    python = os.environ.get("ALPHA_PYTHON", str(ROOT / "work/crewai-venv/bin/python"))
    if not Path(python).is_file():
        raise RuntimeError("Install the external fleet runtime; see integrations/crewai-game-builder/README.md.")
    started = perf_counter()
    native: JSON = {"trace": [], "artifact": "", "tasks": [], "error": None}
    try:
        returned = await run_python(python, HOME / "worker.py", {"roles": variant["roles"], "input": test_case["input"],
                                  "model": backend.requested_model, "python": sys.executable,
                                  "model_binary": backend.binary, "max_calls": 12}, signal, 900)
        if (not isinstance(returned.get("trace"), list) or not isinstance(returned.get("artifact"), str)
                or not isinstance(returned.get("tasks"), list)):
            raise RuntimeError("External runtime returned invalid workflow data.")
        native = returned
        if native.get("error"):
            raise RuntimeError(native["error"])
        verdict = cast(Verdict, await run_python(python, HOME / "grade.py", {"code": native["artifact"], "checks": test_case["checks"]}, signal, 15))
    except Exception as error:
        native["error"] = str(error)
        verdict = {"passed": False, "unsafe": False, "checks": [{"name": "Workflow execution", "passed": False}], "results": []}
    return {"case_id": test_case["id"], "category": test_case["category"], "split": test_case["split"],
            "variant": variant["id"], "trial": trial, "trace": native["trace"],
            "tools": [{"tool": "upstream_task", "arguments": {"agent": t["agent"]}, "result": t["raw"]} for t in native["tasks"]],
            "final_state": {"artifact": native["artifact"], "test_results": verdict.get("results", [])}, "verdict": verdict,
            "error": native.get("error") or verdict.get("execution_error"), "elapsed_ms": (perf_counter() - started) * 1000}
