# Upstream CrewAI Game Builder

This adapter runs the independently authored [Game Builder crew](https://github.com/crewAIInc/crewAI-examples/tree/da94a91e691e1cf5b3151416bb15b5b62729bea8/crews/game-builder-crew) in CrewAI 0.152.0. Its three existing agents write code, inspect it for errors, and review completeness. Alpha adds no agent or deliberate defect.

The selected upstream files are preserved byte for byte. [provenance.json](provenance.json) records the repository commit, source URLs, Git blob identities, and adapter differences. Alpha verifies those identities before every experiment. Attribution and the upstream license declaration are preserved in [NOTICE](NOTICE) and the [original README](upstream/README.md).

## Run

Prerequisites: Node.js 24+, a logged-in Codex CLI, Python 3.11 or 3.12, and macOS for the generated-code sandbox.

```sh
cd /Users/alexr/Alpha
npm run setup:external
npm run external
```

Setup installs into `work/crewai-venv` using the resolved [dependency lock](requirements.lock). Set `ALPHA_BASE_PYTHON` to choose the interpreter used for setup. Set `ALPHA_PYTHON` to use an already installed runtime for experiments. The measured run uses Python 3.12.14, even though the upstream application manifest declares Python `<3.12`; the pinned CrewAI package runs on this interpreter.

## What stays upstream

The adapter imports the original [crew class](upstream/src/game_builder_crew/crew.py) and constructs its native Crew. The [role descriptions](upstream/src/game_builder_crew/config/agents.yaml), [tasks](upstream/src/game_builder_crew/config/tasks.yaml), sequential process, task-context propagation, ReAct parsing, and chief engineer's delegation permission remain upstream behavior. Each trial starts with a fresh Crew and local state directory.

The injected `BaseLLM` adapter transports native CrewAI messages through the existing Codex login. It snapshots messages before the model call and records the returned text and usage. CrewAI interprets that text and executes any native delegation. The model CLI's own shell, web, MCP, and file-change tools are prohibited by the transport. This tests the upstream fleet **with this adapter and model**, not its default GPT-4o configuration.

For removal variants, Alpha removes the selected agent and its task from the constructed Crew. It does not transfer the removed prompt to another agent or insert a cached answer. The surviving native tasks rerun from scratch. The senior engineer remains in all variants.

## Workload and scoring

Eight Alpha-authored headless game-rule tasks specify a `solve(request)` Python API. Discovery uses tic-tac-toe, minesweeper, Mastermind, and blackjack. Holdout uses Connect Four, Reversi, Yahtzee, and Nim. The task specifications are ordinary game requirements; they do not say which agent to remove.

The hidden input/output checks are passed only to the separate grader, never to the crew or model. The grader extracts a Python code block when needed, without repairing its contents, and executes the program in a separate macOS sandbox. Network access and reads outside the system/interpreter/temp allowlist are blocked. CPU time, wall time, and output size are bounded. This is not a universal hostile-code execution service.

Alpha selects a candidate only from discovery, then freezes it before holdout. A no-regression result on eight development tasks remains exploratory evidence. The crew is independent; the tasks and grader are ours. These tasks do not establish production game quality, security, GUI quality, or performance on other models.

Each workflow has a 12-request model budget. Every completed workflow is journaled to `progress.jsonl` so failures and interruptions remain inspectable. Journals are not silently resumed into a new experiment.

## Evidence

Each finished experiment writes `experiment.json`, `report.html`, and a proposed patch to [fleet.config.json](fleet.config.json). The report includes the upstream commit, adapter digest, task/prompt digests, model usage, generated programs, individual executable checks, and input snapshots for every model request. The proposal is not applied automatically.

```sh
npm test
work/crewai-venv/bin/python -m unittest discover -s test -p '*_test.py' -v
```

## Recorded run

The [checked-in report](../../examples/upstream-game-builder/report.html) records 52 real requests. The selected configuration removed the chief QA agent. Both baseline and candidate passed all 42 executable checks across the four holdout games. Baseline used 14 requests and 225,680 reported tokens; the candidate used 8 requests and 128,915 tokens.

The adapter SHA-256 in the result was checked against the files after the run. Exploratory adapter-development runs were not mixed into this recorded experiment.
