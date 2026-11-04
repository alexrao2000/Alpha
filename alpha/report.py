# Reports retain the standalone browser view and version-one embedded JSON. Only
# escaped content enters markup; the template is substituted once to avoid treating
# untrusted values as further template instructions.

from __future__ import annotations

import html
import json
import math
from pathlib import Path
import re
from typing import Any

from .contracts import JSON, compact_json
from .engine import compare, summarize


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def num(value: Any) -> str:
    return "—" if value is None else f"{math.floor(value + 0.5):,}"


def pct(value: Any) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def render_report(report: JSON) -> str:
    fleet = report.get("fleet") or {}
    external = fleet.get("id") == "crewai-game-builder"
    config_path = fleet.get("config_path", "fleet.config.json")
    discovery = [r for r in report["runs"] if r["split"] == "discovery"]
    holdout = [r for r in report["runs"] if r["split"] == "holdout"]
    baseline = summarize([r for r in holdout if r["variant"] == "baseline"])
    candidate = summarize([r for r in holdout if r["variant"] == report["selected"]])
    savings = 1 - candidate["role_calls"] / baseline["role_calls"] if report["selected"] and candidate["count"] and baseline["role_calls"] else None
    baseline_tokens = None if baseline["input_tokens"] is None else baseline["input_tokens"] + baseline["output_tokens"]
    candidate_tokens = None if candidate["input_tokens"] is None else candidate["input_tokens"] + candidate["output_tokens"]
    tokens = 1 - candidate_tokens / baseline_tokens if report["selected"] and baseline_tokens and candidate_tokens is not None else None
    variant = next((v for v in report["variants"] if v["id"] == report["selected"]), None)
    roles = (variant or report["variants"][0])["roles"]
    rows = []
    for v in report["variants"]:
        runs = [r for r in discovery if r["variant"] == v["id"]]
        summary = summarize(runs)
        regressions = 0 if v["id"] == "baseline" else len(compare([r for r in discovery if r["variant"] == "baseline"], runs)["regressions"])
        selected = v["id"] == report["selected"]
        tag = '<span class="tag">SELECTED</span>' if selected else ""
        total = None if summary["input_tokens"] is None else summary["input_tokens"] + summary["output_tokens"]
        rows.append(f'<tr class="{"selected" if selected else ""}"><td><strong>{esc(v["label"])}</strong>{tag}</td><td>{summary["passed"]}/{summary["count"]}</td><td>{num(summary["role_calls"])}</td><td>{num(total)}</td><td class="{"bad" if summary["unsafe"] or regressions else ""}">{regressions}</td><td class="{"bad" if summary["unsafe"] else ""}">{summary["unsafe"]}</td></tr>')
    traces = []
    for run in report["runs"]:
        passed = run["verdict"]["passed"]
        checks = "".join(f'<span class="{"" if c["passed"] else "bad"}">{"✓" if c["passed"] else "×"} {esc(c["name"])}</span>' for c in run["verdict"]["checks"])
        steps = []
        for step in run["trace"]:
            usage = step.get("usage")
            measured = f' · {num(usage["input_tokens"] + usage["output_tokens"])} tokens' if usage else " · scripted"
            output = step.get("output", {"error": step.get("error")})
            steps.append(f'<div class="step"><h4>{esc(step["role"])} <small>{num(step["elapsed_ms"])} ms{measured}</small></h4><div class="step-columns"><div><label>INPUT</label><pre>{esc(pretty(step["context"]))}</pre></div><div><label>OUTPUT</label><pre>{esc(pretty(output))}</pre></div></div></div>')
        error = f'<p class="bad">{esc(run["error"])}</p>' if run.get("error") else ""
        state = esc(pretty({"tools": run["tools"], "state": run["final_state"]}))
        traces.append(f'<details data-split="{esc(run["split"])}" data-variant="{esc(run["variant"])}" data-failed="{str(not passed).lower()}"><summary><span class="dot {"pass" if passed else "fail"}"></span><strong>{esc(run["case_id"])}</strong><span>{esc(run["variant"])}</span><span class="category">{esc(run["category"])} · trial {run["trial"] + 1}</span><b>{"PASS" if passed else "FAIL"}</b></summary><div class="trace-body">{error}<div class="checks">{checks}</div>{"".join(steps)}<div class="step"><h4>Tools and resulting state</h4><pre>{state}</pre></div></div></details>')
    if report["fixture"]:
        notice = "<b>Scripted demonstration.</b> These are functional test doubles with a deliberately redundant planner and an intentionally imperfect resolver. Role-call savings are real counts in this test; they are not model-token, dollar, or production savings."
    elif external:
        notice = "<b>Independent upstream crew.</b> Original source and prompts are verified against pinned Git blob hashes. Native CrewAI controls task context, ReAct tools, and delegation; a Codex-backed model adapter supplies responses. Tasks and executable checks are authored by Alpha. These results concern this model adapter and task suite."
    else:
        notice = "<b>Live model requests.</b> Each role ran through the existing Codex CLI login with isolated context. Reported tokens come from CLI usage events; wall time includes CLI startup. This small authored suite does not establish production equivalence."
    comparison = report.get("comparison")
    metrics = [
        ("HOLDOUT · " + esc(report["selected"] or "BASELINE"), f'{candidate["passed"]}/{candidate["count"]}' if report["selected"] else f'{baseline["passed"]}/{baseline["count"]}', f'{report["dataset"]["holdout_cases"]} cases × {report["trials"]} trials; baseline {baseline["passed"]}/{baseline["count"]}'),
        ("FEWER ROLE INVOCATIONS", pct(savings), "Paired holdout workloads only"),
        ("MODEL TOKEN SAVINGS" if report["fixture"] else "REPORTED TOKEN REDUCTION", "Unmeasured" if report["fixture"] else pct(tokens), "No model calls in fixture mode" if report["fixture"] else "Includes cached input; not billed dollars"),
        ("HOLDOUT REGRESSIONS", str(len(comparison["regressions"])) if comparison else "—", f'{candidate["unsafe"]} unsafe outcomes · {candidate["errors"]} execution errors' if report["selected"] else "No candidate selected"),
    ]
    proposal = f'--- {config_path}\n+++ {config_path}\n- {compact_json(report["variants"][0]["roles"])}\n+ {compact_json(roles)}' if report["selected"] else "Keep the baseline fleet."
    source = ""
    if external:
        provenance = fleet["provenance"]
        url = f'{provenance["repository"]}/tree/{provenance["commit"]}/{provenance["path"]}'
        source = f'Source: <a style="color:inherit" href="{esc(url)}">{esc(fleet["name"])} @ {esc(provenance["commit"])}</a><br>Native runtime: crewai {esc(provenance["runtime"]["version"])}<br>'
    values = {
        "STATUS": "SCRIPTED DEMO" if report["fixture"] else "LIVE MODEL RUN",
        "ID": esc(report["id"][:8]),
        "TITLE": "A smaller fleet.\nMeasured against the same work." if report["validated"] else "Every agent step should earn its place.",
        "INTRO": "Run the pinned upstream CrewAI Game Builder, remove existing agent/task pairs, and reexecute the native workflow. Select on discovery games, then test the frozen candidate on different holdout games." if external else "Run the full fleet, remove one role at a time, and reexecute the remaining workflow. Select on discovery cases, then check the frozen candidate on separate holdout cases.",
        "NOTICE": notice,
        "METRICS": "".join(f'<div class="metric"><span>{label}</span><strong>{value}</strong><small>{caption}</small></div>' for label, value, caption in metrics),
        "PIPELINE": '<span class="arrow">→</span>'.join(f'<div class="node {"" if role in roles else "removed"}">{esc(role)}</div>' for role in report["variants"][0]["roles"]),
        "CAPTION": "No observed regression on this holdout. Review the evidence before applying the proposed change." if report["validated"] else "The experiment did not validate a simplification.",
        "ROWS": "".join(rows),
        "PROPOSAL_DESCRIPTION": "This patch changes the configured role list. It is a proposal; no configuration was applied." if report["selected"] else "No change proposed.",
        "PROPOSAL": esc(proposal),
        "GRADING": "Generated Python programs run in a separate macOS sandbox. Hidden input/output checks test each game’s actual behavior. Expected answers never enter the crew or model context. The grader does not repair generated code." if external else "Actual disposition, ticket state, refund amount and count, refund target, and order state. Expected answers are held outside role context. Policy failures remain visible because the simulated action tool does not repair them.",
        "LIMITATIONS": "The crew is independently authored; the tasks and grader are Alpha-authored. Discovery and holdout use different games. One trial per game is exploratory evidence, not a production equivalence claim." if external else "The suite is authored alongside this prototype. Its holdout varies values and wording within the same scenario families. Small samples and related cases limit generalization.",
        "OPTIONS": "".join(f'<option value="{esc(v["id"])}">{esc(v["label"])}</option>' for v in report["variants"]),
        "TRACES": "".join(traces),
        "FOOTER": f'Alpha 0.1 · {esc(report["created_at"])}<br>{source}Backend: {esc(report["backend"])} · Model: {esc(report.get("model") or "none")}<br>Dataset SHA-256: {esc(report["dataset"]["sha256"])}<br>Prompt SHA-256: {esc(report["prompt_sha256"])}<br>No confidence bound or production safety claim is implied by zero observed failures.',
        "EXPERIMENT": compact_json(report).replace("<", "\\u003c"),
    }
    template = (Path(__file__).parent / "templates/report.html").read_text(encoding="utf-8")
    return re.sub(r"@@([A-Z_]+)@@", lambda match: values[match[1]], template)
