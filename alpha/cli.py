# The CLI journals completed workflows and writes version-one JSON, a standalone
# report, and an unapplied patch. A fresh run always starts its own journal.

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import signal as signals
import sys

from .backends import CodexBackend, FixtureBackend
from .contracts import compact_json
from .domain import load_cases, load_variants
from .engine import experiment, load_prompts, summarize, timestamp
from .external_fleet import load_external_fleet, run_external_case
from .report import render_report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="alpha", description="Measure the value of each agent step.")
    commands = result.add_subparsers(dest="command")
    commands.add_parser("help", help="Show usage")
    run = commands.add_parser("experiment", help="Run a fresh discovery and holdout experiment")
    run.add_argument("--backend", choices=("fixture", "codex"), default="fixture", help="Scripted demo or live CLI requests")
    run.add_argument("--fleet", choices=("crewai-game-builder",))
    run.add_argument("--limit", type=int, help="Cases per split")
    run.add_argument("--trials", type=int, default=2)
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--variants", help="Comma-separated candidate IDs; baseline is always included")
    run.add_argument("--model", help="Optional model override; the existing CLI login is used")
    run.add_argument("--out", type=Path)
    report = commands.add_parser("report", help="Render an existing experiment without executing agents")
    report.add_argument("path", type=Path)
    report.add_argument("--out", type=Path)
    return result


def write_outputs(report, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "experiment.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (out / "report.html").write_text(render_report(report), encoding="utf-8")
    selected = next((v for v in report["variants"] if v["id"] == report["selected"]), None)
    config = (report.get("fleet") or {}).get("config_path", "fleet.config.json")
    proposal = (f"--- a/{config}\n+++ b/{config}\n@@ -1 +1 @@\n"
                f"-{compact_json(report['variants'][0]['roles'])}\n+{compact_json(selected['roles'])}\n") if selected else "No candidate selected.\n"
    (out / "proposal.patch").write_text(proposal, encoding="utf-8")


async def run(args) -> int:
    out = (args.out or Path("runs") / timestamp().replace(":", "-").replace(".", "-")).resolve()
    if args.command == "report":
        report = json.loads(args.path.read_text(encoding="utf-8"))
    else:
        external = load_external_fleet() if args.fleet else None
        if external and args.backend != "codex":
            raise ValueError("The external fleet requires the live codex backend.")
        available = external["variants"] if external else load_variants()
        variants = available
        if args.variants:
            ids = {"baseline", *args.variants.split(",")}
            if ids - {v["id"] for v in available}:
                raise ValueError("Unknown variant ID.")
            variants = [v for v in available if v["id"] in ids]
        backend = FixtureBackend() if args.backend == "fixture" else CodexBackend(model=args.model)
        cancellation = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed = []
        for sig in (signals.SIGINT, signals.SIGTERM):
            try:
                loop.add_signal_handler(sig, cancellation.set)
                installed.append(sig)
            except NotImplementedError:
                pass
        out.mkdir(parents=True, exist_ok=True)
        journal = out / "progress.jsonl"
        journal.write_text("", encoding="utf-8")

        def completed(run):
            print(f"Completed · {run['case_id']} · {run['variant']} · {'PASS' if run['verdict']['passed'] else 'FAIL'}", file=sys.stderr)
            with journal.open("a", encoding="utf-8") as stream:
                stream.write(compact_json(run) + "\n")
                stream.flush()

        try:
            kwargs = {"run_task": run_external_case} if external else {}
            report = await experiment(cases=external["cases"] if external else load_cases(),
                                      prompts=external["prompts"] if external else load_prompts(), backend=backend,
                                      fleet=external["fleet"] if external else None, variants=variants,
                                      trials=args.trials, limit=args.limit, concurrency=args.concurrency,
                                      signal=cancellation, on_progress=lambda message: print(message, file=sys.stderr),
                                      on_run=completed, **kwargs)
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)
    write_outputs(report, out)
    runs = [r for r in report["runs"] if r["split"] == "holdout" and r["variant"] == (report["selected"] or "baseline")]
    print(json.dumps({"backend": report["backend"], "selected": report["selected"], "validated": report["validated"],
                      "holdout": summarize(runs), "report": str(out / "report.html")}, indent=2))
    return 2 if any(r.get("error") for r in report["runs"]) else 0


def main(argv=None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    if args.command in (None, "help"):
        arguments.print_help()
        return 0
    try:
        return asyncio.run(run(args))
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("Alpha: Experiment cancelled. Completed runs remain in progress.jsonl.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Alpha: {error}", file=sys.stderr)
        return 1
