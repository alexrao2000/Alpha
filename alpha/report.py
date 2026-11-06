# Reports retain the standalone browser view and version-one embedded JSON. Only
# escaped content enters markup; the template is substituted once to avoid treating
# untrusted values as further template instructions.

from __future__ import annotations

import html
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit
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
    native = bool(fleet)
    modern = report.get("version", 1) >= 2
    config_path = fleet.get("config_path", "fleet.config.json")
    discovery = [r for r in report["runs"] if r["split"] == "discovery"]
    holdout = [r for r in report["runs"] if r["split"] == "holdout"]
    baseline = summarize([r for r in holdout if r["variant"] == "baseline"])
    candidate = summarize([r for r in holdout if r["variant"] == report["selected"]])
    paired_complete = report.get('status', 'complete') == 'complete'
    savings = 1 - candidate["role_calls"] / baseline["role_calls"] if paired_complete and report["selected"] and candidate["count"] and baseline["role_calls"] else None
    baseline_tokens = None if baseline["input_tokens"] is None else baseline["input_tokens"] + baseline["output_tokens"]
    candidate_tokens = None if candidate["input_tokens"] is None else candidate["input_tokens"] + candidate["output_tokens"]
    tokens = 1 - candidate_tokens / baseline_tokens if paired_complete and report["selected"] and baseline_tokens and candidate_tokens is not None else None
    variant = next((v for v in report["variants"] if v["id"] == report["selected"]), None)
    roles = (variant or report["variants"][0])["roles"]
    rows = []
    for v in report["variants"]:
        runs = [r for r in discovery if r["variant"] == v["id"]]
        summary = summarize(runs)
        base_map = {(r["case_id"], r["trial"]): r for r in discovery if r["variant"] == "baseline"}
        regressions = 0 if v["id"] == "baseline" else sum(base_map.get((r["case_id"], r["trial"]), {}).get("verdict", {}).get("passed", False) and not r["verdict"]["passed"] for r in runs)
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
            measured = f' · {num(usage["input_tokens"] + usage["output_tokens"])} tokens' if usage else " · " + esc(step.get("execution", "scripted" if report["fixture"] else "usage unavailable"))
            output = step.get("output", {"error": step.get("error")})
            parents = ' · parents: ' + ', '.join(step.get('parent_ids', [])) if modern and step.get('parent_ids') else ''
            steps.append(f'<div class="step"><h4>{esc(step["role"])} <small>{num(step["elapsed_ms"])} ms{measured}{esc(parents)}</small></h4><div class="step-columns"><div><label>INPUT</label><pre>{esc(pretty(step["context"]))}</pre></div><div><label>OUTPUT</label><pre>{esc(pretty(output))}</pre></div></div></div>')
        error = f'<p class="bad">{esc(run["error"])}</p>' if run.get("error") else ""
        state = esc(pretty({"tools": run["tools"], "state": run["final_state"]}))
        traces.append(f'<details data-split="{esc(run["split"])}" data-variant="{esc(run["variant"])}" data-failed="{str(not passed).lower()}"><summary><span class="dot {"pass" if passed else "fail"}"></span><strong>{esc(run["case_id"])}</strong><span>{esc(run["variant"])}</span><span class="category">{esc(run["category"])} · trial {run["trial"] + 1}</span><b>{"PASS" if passed else "FAIL"}</b></summary><div class="trace-body">{error}<div class="checks">{checks}</div>{"".join(steps)}<div class="step"><h4>Tools and resulting state</h4><pre>{state}</pre></div></div></details>')
    if report["fixture"]:
        notice = "<b>Scripted demonstration.</b> These are functional test doubles with a deliberately redundant planner and an intentionally imperfect resolver. Role-call savings are real counts in this test; they are not model-token, dollar, or production savings."
    elif native:
        notice = "<b>Independent upstream crew.</b> Original source and prompts are verified against pinned Git blob hashes. Native CrewAI controls task context, ReAct tools, and delegation; a Codex-backed model adapter supplies responses. Tasks and executable checks are authored by Alpha. These results concern this model adapter and task suite."
    else:
        notice = "<b>Live model requests.</b> Each role ran through the existing Codex CLI login with isolated context. Reported tokens come from CLI usage events; wall time includes CLI startup. This small authored suite does not establish production equivalence."
    if native and not external:
        notice = "<b>Adapter workflow.</b> Declared file identities are verified before execution. The adapter declares runtime and transport differences. Private expected outcomes are passed only to the separate grader."
        if report['fixture']:
            notice += ' <b>Scripted demonstration: no model quality, token, or cost measurement.</b>'
    comparison = report.get("comparison")
    metrics = [
        ("HOLDOUT · " + esc(report["selected"] or "BASELINE"), f'{candidate["passed"]}/{candidate["count"]}' if report["selected"] else f'{baseline["passed"]}/{baseline["count"]}', f'{report["dataset"]["holdout_cases"]} cases × {report["trials"]} trials; baseline {baseline["passed"]}/{baseline["count"]}'),
        ("FEWER ROLE INVOCATIONS", pct(savings), "Paired holdout workloads only"),
        ("MODEL TOKEN SAVINGS" if report["fixture"] else "REPORTED TOKEN REDUCTION", "Unmeasured" if report["fixture"] else pct(tokens), "No model calls in fixture mode" if report["fixture"] else "Includes cached input; not billed dollars"),
        ("HOLDOUT REGRESSIONS", str(len(comparison["regressions"])) if comparison else "—", f'{candidate["unsafe"]} unsafe outcomes · {candidate["errors"]} execution errors' if report["selected"] else "No candidate selected"),
    ]
    proposal = f'--- {config_path}\n+++ {config_path}\n- {compact_json(report["variants"][0]["roles"])}\n+ {compact_json(roles)}' if report["selected"] else "Keep the baseline fleet."
    source = ""
    if native and fleet.get("provenance"):
        provenance = fleet["provenance"]
        url = f'{provenance["repository"]}/tree/{provenance["commit"]}/{provenance.get("path", "")}'
        title = f'{esc(fleet["name"])} @ {esc(provenance["commit"])}'
        parsed = urlsplit(url)
        source_link = f'<a style="color:inherit" href="{esc(url)}">{title}</a>' if parsed.scheme in ('https', 'http') and parsed.netloc else title
        source = f'Source: {source_link}<br>Runtime details: {esc(provenance.get("runtime", {}).get("version", "adapter manifest"))}<br>'
    decision_html, tradeoffs_html = '', ''
    if modern:
        status = report.get('status', 'complete')
        reasons = (report.get('assessments', {}).get(report['selected']) or {}).get('reasons', [])
        if report.get('stop_reason'):
            reasons = [report['stop_reason'], *reasons]
        if not reasons:
            reasons = ['The selected candidate meets the predeclared task-level regression bound.'] if report['validated'] else ['No validated change is recommended.']
        decision_html = '<div class="notice"><b>' + esc(report.get('decision', 'inconclusive').upper()) + ' · ' + esc(status) + '</b><br>' + '<br>'.join(esc(r) for r in reasons) + '</div>'
        policy = report.get('policy', {})
        decision_html += '<p class="caption">Task-level confidence: ' + pct(policy.get('confidence')) + '; maximum tolerated regression probability: ' + pct(policy.get('max_regression_rate')) + '. Repeated trials and assertions do not increase the independent task count. Comparisons share the confidence error budget.</p>'
        assessment_rows = []
        for candidate_id, assessment in report.get('assessments', {}).items():
            assessment_rows.append('<tr><td>' + esc(candidate_id) + '</td><td>' + esc(assessment['decision']) + '</td><td>' + str(assessment['independent_units']) + '</td><td>' + str(assessment['regressed_units']) + '</td><td>' + pct(assessment['regression_upper_bound']) + '</td></tr>')
        if assessment_rows:
            decision_html += '<div class="table-wrap"><table><thead><tr><th>FROZEN CANDIDATE</th><th>DECISION</th><th>TASK UNITS</th><th>REGRESSED UNITS</th><th>UPPER REGRESSION BOUND</th></tr></thead><tbody>' + ''.join(assessment_rows) + '</tbody></table></div>'
        tradeoff_rows = []
        recommendations = report.get('recommendations', {})
        for row in report.get('tradeoffs', []):
            labels = ', '.join(k.replace('_', ' ') for k, v in recommendations.items() if v == row['id'])
            cost = 'Unknown' if row.get('estimated_cost_usd') is None else f"${row['estimated_cost_usd']:.6f} estimated"
            badge = ' · frontier' if row['id'] in report.get('frontier', []) else ''
            tradeoff_rows.append('<tr><td>' + esc(row['label']) + '<br><small>' + esc(labels + badge) + '</small></td><td>' + pct(row['quality']) + '</td><td>' + num(row.get('tokens_per_success')) + '</td><td>' + cost + '</td><td>' + num(row['median_ms']) + '</td><td>' + num(row['p95_ms']) + '</td></tr>')
        accounting = report.get('accounting', {})
        tradeoffs_html = '<h2>Quality, cost, and latency</h2><div class="table-wrap"><table><thead><tr><th>WORKFLOW</th><th>QUALITY</th><th>TOKENS / SUCCESS</th><th>USD / SUCCESS</th><th>MEDIAN MS</th><th>P95 MS</th></tr></thead><tbody>' + ''.join(tradeoff_rows) + '</tbody></table></div>'
        tradeoffs_html += '<p class="caption">Labels were frozen on discovery; holdout does not choose a new winner. Only validated candidates enter the live frontier. Missing costs stay unknown and cannot establish dominance. USD values use supplied price estimates, never billing records.</p>'
        tradeoffs_html += '<div class="notice">Admitted model requests: ' + num(accounting.get('attempted_calls', 0)) + ' · failed: ' + num(accounting.get('failed_calls', 0)) + ' · interrupted: ' + num(accounting.get('interrupted_calls', 0)) + ' · unknown usage: ' + num(accounting.get('unknown_usage_attempts', 0)) + '<br>Active seconds: ' + num(accounting.get('active_seconds', 0)) + '. Reported token ceilings stop new requests after observed usage reaches the threshold; in-flight work can overshoot.</div>'
    values = {
        "STATUS": "SCRIPTED DEMO" if report["fixture"] else "LIVE MODEL RUN",
        "ID": esc(report["id"][:8]),
        "TITLE": "A smaller fleet.\nMeasured against the same work." if report["validated"] else "Every agent step should earn its place.",
        "INTRO": "Run the pinned upstream CrewAI Game Builder, remove existing agent/task pairs, and reexecute the native workflow. Select on discovery games, then test the frozen candidate on different holdout games." if external else "Run the full fleet, remove one role at a time, and reexecute the remaining workflow. Select on discovery cases, then check the frozen candidate on separate holdout cases.",
        "NOTICE": notice, "DECISION": decision_html, "TRADEOFFS": tradeoffs_html,
        "METRICS": "".join(f'<div class="metric"><span>{label}</span><strong>{value}</strong><small>{caption}</small></div>' for label, value, caption in metrics),
        "PIPELINE": '<span class="arrow">→</span>'.join(f'<div class="node {"" if role in roles else "removed"}">{esc(role)}</div>' for role in report["variants"][0]["roles"]),
        "CAPTION": "No observed regression on this holdout. Review the evidence before applying the proposed change." if report["validated"] else "The experiment did not validate a simplification.",
        "ROWS": "".join(rows),
        "SELECTION": 'Discovery follows the predeclared objective among candidates within the observed quality tolerance and floor, without unsafe outcomes or execution errors. All eligible recipes are frozen before holdout.' if modern else 'The discovery winner minimizes observed tokens per successful task (role calls in fixture mode), among candidates with no paired regression or unsafe outcome. Holdout results never influence selection.',
        "PROPOSAL_DESCRIPTION": "The complete candidate recipe is in candidate-config.json. Inconclusive live results do not produce an applicable patch." if modern else "This patch changes the configured role list. It is a proposal; no configuration was applied." if report["selected"] else "No change proposed.",
        "PROPOSAL": esc(proposal),
        "GRADING": "Generated Python programs run in a separate macOS sandbox. Hidden input/output checks test each game’s actual behavior. Expected answers never enter the crew or model context. The grader does not repair generated code." if external else "Actual disposition, ticket state, refund amount and count, refund target, and order state. Expected answers are held outside role context. Policy failures remain visible because the simulated action tool does not repair them.",
        "LIMITATIONS": "The crew is independently authored; the tasks and grader are Alpha-authored. Discovery and holdout use different games. One trial per game is exploratory evidence, not a production equivalence claim." if external else "The suite is authored alongside this prototype. Its holdout varies values and wording within the same scenario families. Small samples and related cases limit generalization.",
        "OPTIONS": "".join(f'<option value="{esc(v["id"])}">{esc(v["label"])}</option>' for v in report["variants"]),
        "TRACES": "".join(traces),
        "FOOTER": f'Alpha 0.1 · {esc(report["created_at"])}<br>{source}Backend: {esc(report["backend"])} · Model: {esc(report.get("model") or "none")}<br>Dataset SHA-256: {esc(report["dataset"]["sha256"])}<br>Prompt SHA-256: {esc(report["prompt_sha256"])}<br>No confidence bound or production safety claim is implied by zero observed failures.',
        "EXPERIMENT": compact_json(report).replace("<", "\\u003c"),
    }
    if modern and report['selected']:
        values['PROPOSAL'] = esc(pretty(variant))
    if modern:
        values['FOOTER'] = values['FOOTER'].replace('No confidence bound or production safety claim is implied by zero observed failures.', 'Exact bounds assume independent, representative task units. A validated development comparison is not a production safety claim.')
        values['INTRO'] = 'Reexecute complete workflows from fresh state. Compare removals, review rules, context, prompts, and model choices on discovery; freeze the choices before holdout.'
    if native and not external:
        values['INTRO'] = 'Execute a pinned or user-supplied native workflow with fresh state. Freeze candidate recipes on discovery, then grade holdout behavior separately.'
        values['GRADING'] = 'An adapter-specific executable grader receives the artifact and private checks after the workflow exits. Expected outcomes never enter the workflow or model gateway.'
        values['LIMITATIONS'] = 'Bundled cases are development suites. The adapter declares exactly what its rubric checks; functional checks do not establish unmeasured semantic quality.'
    template = (Path(__file__).parent / "templates/report.html").read_text(encoding="utf-8")
    return re.sub(r"@@([A-Z_]+)@@", lambda match: values[match[1]], template)
