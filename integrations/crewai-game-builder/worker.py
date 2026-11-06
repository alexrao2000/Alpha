# The upstream class, prompts, tasks, ordering, and delegation flags stay intact.
# Only Agent's model transport is injected; removal variants filter the resulting
# native Crew's existing agent/task pairs before kickoff. Oracles never enter here.

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
request = json.load(sys.stdin)
os.environ.update({"OTEL_SDK_DISABLED": "true", "CREWAI_TELEMETRY_ENABLED": "false", "DO_NOT_TRACK": "1"})

with tempfile.TemporaryDirectory(prefix="alpha-crew-") as state_dir:
    os.environ["CREWAI_STORAGE_DIR"] = state_dir
    os.environ["XDG_DATA_HOME"] = state_dir
    sys.path.insert(0, str(HERE / "upstream" / "src"))
    with contextlib.redirect_stdout(sys.stderr):
        from crewai import Agent, BaseLLM
        import game_builder_crew.crew as upstream

    traces = []
    total_calls = [0]

    class RecordingLLM(BaseLLM):
        def __init__(self, role):
            super().__init__(model=request.get("models", {}).get(role) or request.get("model") or "codex-cli-default", temperature=0)
            self.alpha_role = role

        def supports_function_calling(self):
            return False

        def supports_stop_words(self):
            return False

        def get_context_window_size(self):
            return 128000

        def call(self, messages, tools=None, callbacks=None, available_functions=None, **kwargs):
            snapshot = json.loads(json.dumps(messages))
            total_calls[0] += 1
            if total_calls[0] > request.get("max_calls", 12):
                raise RuntimeError("The workflow exceeded its model-request budget.")
            started = time.monotonic()
            print(f"ALPHA_MODEL {self.alpha_role} start {total_calls[0]}", file=sys.stderr, flush=True)
            result = subprocess.run(
                [request["python"], "-m", "alpha.model_bridge"],
                input=json.dumps({"messages": snapshot, "model": request.get("model"), "model_binary": request.get("model_binary", "codex"), "gateway": request.get("gateway"), "role": self.alpha_role}),
                text=True, capture_output=True, timeout=135, cwd=ROOT,
            )
            elapsed = (time.monotonic() - started) * 1000
            if result.returncode:
                traces.append({"role": self.alpha_role, "context": {"messages": snapshot},
                               "error": "Model transport failed", "elapsed_ms": elapsed,
                               "usage": None, "model_calls": 0})
                raise RuntimeError("The model transport failed. Inspect local diagnostics.")
            reply = json.loads(result.stdout)
            traces.append({"role": self.alpha_role, "context": {"messages": snapshot},
                           "output": {"response": reply["response"]}, "elapsed_ms": elapsed,
                           "usage": reply["usage"], "model_calls": reply["model_calls"], "attempt_id": reply.get("attempt_id"),
                           "id": f"step-{len(traces)}", "parent_ids": [f"step-{len(traces)-1}"] if traces else [], "model": self.model})
            print(f"ALPHA_MODEL {self.alpha_role} done", file=sys.stderr, flush=True)
            return reply["response"]

    role_map = {
        "Senior Software Engineer": "senior_engineer_agent",
        "Software Quality Control Engineer": "qa_engineer_agent",
        "Chief Software Quality Control Engineer": "chief_qa_engineer_agent",
    }

    def configured_agent(*args, **kwargs):
        role = role_map[kwargs["config"]["role"].strip()]
        if role in request.get("prompt_overrides", {}):
            kwargs["config"] = {**kwargs["config"], "goal": request["prompt_overrides"][role]}
        return Agent(*args, llm=RecordingLLM(role), **kwargs)

    upstream.Agent = configured_agent
    error, raw, task_results = None, "", []
    try:
        with contextlib.redirect_stdout(sys.stderr):
            crew = upstream.GameBuilderCrew().crew()
            keep = set(request["roles"])
            crew.agents = [a for a in crew.agents if role_map[a.role.strip()] in keep]
            crew.tasks = [t for t in crew.tasks if role_map[t.agent.role.strip()] in keep]
            result = crew.kickoff(inputs={"game": request["input"]["game"]})
            raw = result.raw
            task_results = [{"agent": t.agent, "raw": t.raw} for t in result.tasks_output]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    print(json.dumps({"trace": traces, "artifact": raw, "tasks": task_results, "error": error}))
