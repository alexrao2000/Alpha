# Alpha

Alpha is a local prototype for measuring the value of individual steps in an agent fleet. It reexecutes complete workflows from fresh state, compares candidate changes, freezes the choices on discovery tasks, and measures their quality, cost, and latency on holdout tasks. Statistical uncertainty can make the result inconclusive.

The first fleet handles simulated customer support. Its five roles are **planner → researcher → policy → resolver → reviewer**. Each role has its own prompt and output contract. Research chooses an order to look up; the host executes that tool. The final proposed action changes a fresh local order database. An independent grader checks the resulting state.

## New experiment capabilities

- **Uncertainty-aware decisions:** exact task-level confidence bounds, repeated trials, imperfect baselines, and explicit inconclusive results.
- **Bring your own fleet:** a command adapter protocol with separate workflow and grader processes, plus three pinned native workflows.
- **Richer candidates:** role removal, deterministic or conditional review, prompt and context changes, per-role model routing, and combinations.
- **Tradeoffs:** frozen cheapest, fastest, and highest-quality choices, a validated frontier, cached tokens, failed attempts, and explicit price estimates.
- **Long runs:** shared request, observed-token, and deadline budgets; durable checkpoints; explicit hash-checked resume.

See the [experiment guide](docs/experiments.md) and [adapter protocol](docs/adapters.md). Try the expanded scripted search with `python3 -m alpha experiment --backend fixture --search expanded`. These tests remain scripted; they do not establish model savings.

## Test an independently built fleet

Alpha now runs the published [CrewAI Game Builder](https://github.com/crewAIInc/crewAI-examples/tree/da94a91e691e1cf5b3151416bb15b5b62729bea8/crews/game-builder-crew) through its native runtime. Its engineer, QA engineer, and chief QA are original upstream roles. Source and prompts remain byte-identical to the pinned revision; no redundant role or defect is planted.

```sh
npm run setup:external
npm run external
```

The experiment compares the original crew with removal of either review role, selects on four discovery games, and tests the fixed candidate on four different holdout games. Generated programs are checked by executable input/output tests. See the [adapter documentation](integrations/crewai-game-builder/README.md) for setup, provenance, model-transport differences, and limits. Two additional pinned native examples cover [language handoffs](integrations/swarm-handoff/README.md) and a [writing/review retry loop](integrations/crewai-self-review/README.md). Both use the same optional native environment and shared model gateway. The original support fleet below remains a scripted development example.

## Measured upstream results

The [saved upstream experiment](examples/upstream-game-builder/report.html) made 52 live model requests across four discovery games and four different holdout games. Alpha selected removal of the chief QA agent using discovery results only.

| Holdout result | Original crew | Remove chief QA |
| --- | --- | --- |
| Games passed | 4/4 | 4/4 |
| Executable checks passed | 42/42 | 42/42 |
| Model requests | 14 | 8 |
| Reported input + output tokens | 225,680 | 128,915 |
| Median workflow wall time | 62.0 seconds | 46.4 seconds |

On these holdout tasks the selected version used 42.9% fewer model requests and 42.9% fewer reported tokens. Native delegation made the original crew issue more than one request per role on one game. The earlier discovery run also showed that removing the QA engineer could increase requests: the chief delegated work back to the senior engineer.

This is one trial per game with a Codex-backed model adapter. The upstream crew is independently authored; the game specifications and executable checks are Alpha-authored. The result supports this small workload, not a general claim that the chief QA role is redundant. CLI usage includes cached tokens and transport overhead; these are not billed-dollar savings.

## Run the prototype

The experiment engine, CLI, model transport, and HTML report renderer use Python 3.11 or newer with no third-party runtime dependencies. Run these commands from the repository checkout. Node.js 24 or newer is needed only for the optional eve agent-authoring runtime.

```sh
python3 -m alpha experiment --backend fixture
```

Open the generated `report.html` in the output directory. The report includes discovery comparisons, a frozen holdout candidate, a proposed configuration patch, and expandable per-role inputs, outputs, tool events, and final states. It works as a standalone file; no server is needed.

The checked-in demonstration is [examples/fixture/report.html](examples/fixture/report.html). **Fixture mode uses scripted test doubles.** It demonstrates the mechanics of the experiment, including useful negative controls. It does not measure model performance, token savings, or API cost.

## Run real model agents

Install the Codex CLI and sign in with your existing account. Alpha invokes the CLI through its documented `exec` command; it never reads or copies authentication files.

```sh
python3 -m alpha experiment --backend codex --limit 2 --trials 1 \
  --variants without-planner,without-policy
```

`npm run live` runs a smaller smoke comparison: one discovery case, one holdout case, one trial, and two removal candidates.

Each role is a separate model request with isolated context and structured JSON output. The backend uses an ephemeral, read-only temporary working directory and rejects a trial if the model attempts a shell, web, file-change, or MCP tool action. Only the local host executes the simulated order tools. Nothing connects to a real support or payment account.

CLI options include `--model`, `--limit`, `--trials`, `--variants`, `--concurrency`, and `--out`. Use `python3 -m alpha experiment --help` for details. A full live experiment can make hundreds of requests. Begin with the small command above. Roles within a workflow run sequentially. Use `--concurrency 3` to evaluate independent workflows together; each still has its own state. Trial/variant order rotates to reduce a fixed ordering bias. This does not eliminate service-load or caching effects.

Live reports record the usage events returned by the CLI. Input tokens include cached input, which is also reported separately. Optional supplied prices produce estimates that distinguish cached input, output, and chargeable tools. Billing dollars remain unknown. Latency includes process startup. Without `--model`, model identity is labeled as the CLI default rather than a verified provider identity.

## First measured results

The checked-in [live report](examples/live/report.html) contains 22 real model requests across one discovery case and one holdout case, with one trial each. Both cases are variants of the standard-refund scenario; this is a smoke test of the end-to-end experiment.

| Live workflow | Discovery | Holdout | Holdout model calls | Holdout reported tokens |
| --- | --- | --- | --- | --- |
| Full fleet | Pass | Pass | 5 | 79,187 |
| Remove planner | Pass | Pass | 4 | 67,651 |
| Remove policy | Fail | Not selected | — | — |

The selected workflow used 20% fewer calls and 14.6% fewer reported tokens on the single holdout task. Wall time was 177.7 seconds versus 193.6 seconds, including CLI startup and service latency. These observations establish that the prototype runs and detects a harmful removal. They do not establish production savings, statistical equivalence, or the value of pruning an independently built agent.

The separate scripted demonstration evaluates all 32 authored cases twice. It preserves every checked outcome when removing the planner, while removing the reviewer produces excess refunds on two scenario families.

## Fleet authoring and eve

The root and five specialists are authored as an [eve project](https://eve.dev/docs/subagents). Prompts live in `agent/subagents/<role>/instructions.md`; each specialist has an `agent.ts` manifest. The local experiment harness reads those same prompts and runs their contracts through the selected backend. **The Codex adapter tests the role prompts and host workflow, not eve's session or delegation runtime.**

After installing framework dependencies, `npm run build -- --skip-sandbox-prewarm` validates the eve project without preparing a deployment sandbox and `npm run dev` opens its interactive runtime. The default eve manifests use an AI Gateway model string; authentication is managed by eve. The benchmark can run without installing eve or configuring a new provider account.

The Python engine lives in [alpha/](alpha/). Typed contracts describe runs, model usage, variants, and experiment results. External fleets run in separate processes so pinned dependencies and mutable framework state remain isolated. The native Python fleet calls the Python model transport directly. Browser JavaScript handles only report filtering and downloads. The old `node src/cli.mjs` command remains a thin compatibility launcher.

The selected role list lives in [fleet.config.json](fleet.config.json). Proposed patches apply to that file, and the CLI reads it on its next experiment. Alpha never applies the proposed patch automatically. Advanced candidate recipes are recorded in `candidate-config.json` and can be reused with `--candidates`. Arbitrary code surgery and automatic parallelization remain outside the built-in search.

## Experiment boundaries

- **32 authored cases:** 16 discovery and 16 holdout cases cover refund windows, partial and excessive requests, cancellation states, ownership mismatches, risk escalation, and missing orders.
- **Selection:** evaluate baseline and all candidate variants on discovery. Allow an imperfect baseline while excluding unsafe, failed, zero-quality, or out-of-tolerance candidates. Rank by the predeclared objective, then freeze all eligible recipes and discovery labels.
- **Holdout:** freeze the primary choice and eligible candidate set before evaluating them against the baseline on holdout. Decisions use simultaneous task-level confidence bounds; insufficient evidence is inconclusive. Holdout cannot select a new winner.
- **State checks:** verify disposition, ticket state, refund amount and count, refund target, and order states. Role messages do not contain expected answers. The action tool deliberately does not enforce business eligibility, so it cannot conceal a broken policy step.
- **Known construction:** the planner is deliberately redundant. The resolver proposes the requested refund amount, leaving the balance cap to the reviewer. Removing the reviewer therefore exposes excess-refund cases. This is a useful test of Alpha's mechanics, not an independent discovery about an unrelated agent implementation.
- **Generalization:** the holdout changes values and wording within the same scenario families. Both splits were authored alongside the prototype. Repeated fixture trials are identical; small live samples do not establish production equivalence or a low rare-error rate.

## Checks

```sh
python3 -m unittest discover -s test -p '*_test.py' -v
python3 -m alpha experiment --backend fixture
git apply --check examples/fixture/proposal.patch
```

The existing `npm test`, `npm run demo`, `npm run live`, and `npm run external` commands launch Python. Set `ALPHA_CORE_PYTHON` to choose their interpreter. You can also install the package with `python3 -m pip install .` and use the `alpha` command.

Tests cover state isolation, hidden expected outcomes, downstream reexecution, positive and negative controls, paired comparisons, selection leakage, provider failures, malformed responses, configuration changes, and HTML escaping.

## Open-source options considered

[AutoGen](https://github.com/microsoft/autogen) provides substantial multi-agent examples, and [τ-bench](https://github.com/sierra-research/tau2-bench) provides richer tool-agent-user environments. Neither was copied into Alpha. Building the small fleet gave us a controlled first test without installing a large application or running a second user-simulator model. The three native integrations exercise different workflow patterns. Broader independent benchmarks remain future work; all bundled task suites are development data.

The checked-in measured runs predate the Python migration and retain their original evidence and adapter digests. Migration checks compare all 256 scripted workflows against the saved JavaScript fixture and exercise the native fleet with a scripted transport; they are not new model-performance measurements.

Alpha's own code and authored cases are MIT licensed. Dependency licenses remain their own.

Current version-two reports add confidence bounds, frozen tradeoffs, and request accounting. Historical examples stay unchanged and do not acquire stronger statistical claims from the new implementation.
