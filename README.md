# Alpha

Alpha is a local prototype for measuring the value of individual steps in an agent fleet. It runs a complete workflow, removes roles, reexecutes the remaining workflow, and proposes a smaller configuration when it observes no regression.

The first fleet handles simulated customer support. Its five roles are **planner → researcher → policy → resolver → reviewer**. Each role has its own prompt and output contract. Research chooses an order to look up; the host executes that tool. The final proposed action changes a fresh local order database. An independent grader checks the resulting state.

## Run the prototype

The experiment runner has no third-party dependencies and works with Node.js 20 or newer. The optional eve runtime requires Node.js 24 or newer.

```sh
cd /Users/alexr/Alpha
node src/cli.mjs experiment --backend fixture
```

Open the generated `report.html` in the output directory. The report includes discovery comparisons, a frozen holdout candidate, a proposed configuration patch, and expandable per-role inputs, outputs, tool events, and final states. It works as a standalone file; no server is needed.

The checked-in demonstration is [examples/fixture/report.html](examples/fixture/report.html). **Fixture mode uses scripted test doubles.** It demonstrates the mechanics of the experiment, including useful negative controls. It does not measure model performance, token savings, or API cost.

## Run real model agents

Install the Codex CLI and sign in with your existing account. Alpha invokes the CLI through its documented `exec` command; it never reads or copies authentication files.

```sh
node src/cli.mjs experiment --backend codex --limit 2 --trials 1 \
  --variants without-planner,without-policy
```

Each role is a separate model request with isolated context and structured JSON output. The backend uses an ephemeral, read-only temporary working directory and rejects a trial if the model attempts a shell, web, file-change, or MCP tool action. Only the local host executes the simulated order tools. Nothing connects to a real support or payment account.

CLI options include `--model`, `--limit`, `--trials`, `--variants`, `--concurrency`, and `--out`. Use `node src/cli.mjs help` for details. A full live experiment can make hundreds of requests. Begin with the small command above. Roles within a workflow run sequentially. Use `--concurrency 3` to evaluate independent workflows together; each still has its own state. Trial/variant order rotates to reduce a fixed ordering bias. This does not eliminate service-load or caching effects.

Live reports record the usage events returned by the CLI. Input tokens include cached input, which is also reported separately. Alpha does not convert usage into billed dollars. Latency includes process startup. Without `--model`, model identity is labeled as the CLI default rather than a verified provider identity.

## Fleet authoring and eve

The root and five specialists are authored as an [eve project](https://eve.dev/docs/subagents). Prompts live in `agent/subagents/<role>/instructions.md`; each specialist has an `agent.ts` manifest. The local experiment harness reads those same prompts and runs their contracts through the selected backend. **The Codex adapter tests the role prompts and host workflow, not eve's session or delegation runtime.**

After installing framework dependencies, `npm run build` validates the eve project and `npm run dev` opens its interactive runtime. The default eve manifests use an AI Gateway model string; authentication is managed by eve. The benchmark can run without installing eve or configuring a new provider account.

The selected role list lives in [fleet.config.json](fleet.config.json). Proposed patches apply to that file, and the CLI reads it on its next experiment. Alpha never applies the proposed patch automatically. It currently proposes role removal only; prompt rewrites, model routing, parallelization, and arbitrary code surgery are outside this prototype.

## Experiment boundaries

- **32 authored cases:** 16 discovery and 16 holdout cases cover refund windows, partial and excessive requests, cancellation states, ownership mismatches, risk escalation, and missing orders.
- **Selection:** evaluate baseline and all candidate variants on discovery. Require a clean, fully passing baseline, no paired regression, no unsafe outcome, and no execution error. Rank eligible candidates by measured tokens per successful task, or role invocations in fixture mode.
- **Holdout:** freeze the selected candidate before evaluating the baseline and candidate on holdout. Holdout outcomes cannot change which candidate was selected.
- **State checks:** verify disposition, ticket state, refund amount and count, refund target, and order states. Role messages do not contain expected answers. The action tool deliberately does not enforce business eligibility, so it cannot conceal a broken policy step.
- **Known construction:** the planner is deliberately redundant. The resolver proposes the requested refund amount, leaving the balance cap to the reviewer. Removing the reviewer therefore exposes excess-refund cases. This is a useful test of Alpha's mechanics, not an independent discovery about an unrelated agent implementation.
- **Generalization:** the holdout changes values and wording within the same scenario families. Both splits were authored alongside the prototype. Repeated fixture trials are identical; small live samples do not establish production equivalence or a low rare-error rate.

## Checks

```sh
npm test
npm run demo
git apply --check examples/fixture/proposal.patch
```

Tests cover state isolation, hidden expected outcomes, downstream reexecution, positive and negative controls, paired comparisons, selection leakage, provider failures, malformed responses, configuration changes, and HTML escaping.

## Open-source options considered

[AutoGen](https://github.com/microsoft/autogen) provides substantial multi-agent examples, and [τ-bench](https://github.com/sierra-research/tau2-bench) provides richer tool-agent-user environments. Neither was copied into Alpha. Building the small fleet gave us a controlled first test without installing a large application or running a second user-simulator model. Integrating an independently maintained fleet or benchmark is the next external validation step.

Alpha's own code and authored cases are MIT licensed. Dependency licenses remain their own.
