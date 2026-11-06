# Bring an existing workflow

`python3 -m alpha adapters` lists bundled fleets. `--adapter /path/to/adapter.json` accepts a workflow without changing the engine. A complete, dependency-free scripted example lives in [examples/adapter](../examples/adapter). Run it with:

```sh
python3 -m alpha experiment --adapter examples/adapter/adapter.json --trials 2
```

Its normalization and formatting stages both contribute. Removing normalization fails. It demonstrates the protocol, fresh initial files, separate grading, and failure detection; it makes no model calls.

## Manifest and resources

A version-one manifest declares `id`, `name`, `cases`, `variants`, `runner`, `grader`, and `files`. `scripted: true` requires fixture mode; native model workflows require the codex backend. `files` lists the dataset and every source/configuration resource used by the adapter. Paths must resolve inside the adapter directory. SHA-256 identities of all declared files and the entire manifest enter the resume fingerprint; optional `pins` map paths to required hashes. Declared identities are rechecked before each workflow. Undeclared dynamic dependencies are the adapter author's responsibility.

Commands are arrays beginning with `{python}` (the core interpreter) or `{fleet_python}` (the native interpreter), followed by a declared Python script and optional arguments. Set `ALPHA_PYTHON` to choose the native interpreter; the default is the project-local environment created by `npm run setup:external`. Native interpreter bytes and installed package versions enter the fingerprint. `timeout_seconds` defaults to 900 for the workflow; graders have a 30-second limit. See the [example manifest](../examples/adapter/adapter.json).

Declare transformation `capabilities` from `models`, `prompt_overrides`, `drop_context`, `deterministic`, and `conditional`, and a `rules` map from role ID to allowed rule names. The runner must implement every advertised capability. The baseline recipe starts with `id: baseline`; other IDs must be unique, and roles are ordered subsets of its roles. Unknown settings are rejected.

## Dataset

The dataset is a JSON list. Each case has a unique `id`, `split` (`discovery` or `holdout`), and visible `input` object. Optional `initial` contains visible initial state; `initial.files` maps safe relative file paths to UTF-8 content copied into a fresh working directory for **every** case/variant/trial. Optional `category` is a display label; `evaluation_unit` groups dependent cases for inference. Private `checks` and `expected` go only to the grader.

## Worker request and response

The runner reads one JSON object from standard input:

```json
{"input":{},"initial":{},"variant":{"id":"baseline","label":"Original","roles":["writer","reviewer"]},"prompts":{},"gateway":{"socket":"...","lease":"..."},"core_python":"...","model":"..."}
```

Neither expected outcomes nor discovery/holdout labels are sent. The worker runs native control flow and emits one JSON object to stdout. Send runtime logs to stderr; Alpha discards raw diagnostics.

```json
{"artifact":"produced text or source","trace":[{"id":"s0","parent_ids":[],"role":"writer","context":{},"output":{},"elapsed_ms":1,"usage":null,"model_calls":0,"execution":"deterministic"}],"tools":[],"final_state":{}}
```

Artifacts are strings and state is an object. Each trace step declares a configured role, context object, model call count, timing, ID, and parent IDs. Steps must be topologically ordered with unique IDs and nonnegative finite timings. Repeated role invocations use different step IDs. Branches and joins are allowed. Scripted steps have zero model calls and `usage: null`.

For model calls, import `alpha.sdk.complete` for structured output or `alpha.sdk.conversation` for original native chat messages. The host supplies `PYTHONPATH`; the native environment needs no second Alpha installation. `alpha.sdk.step` records the returned usage and `attempt_id`. Every live call must use its current workflow gateway and correspond one-to-one to a recorded attempt. The gateway binds the role and model override to the host job, shares budgets, and keeps a write-ahead ledger. Worker-produced usage is not authoritative for cost; the host ledger is. Unix-domain sockets currently require macOS or Linux for native gateway adapters.

Tools may report `tool`, `arguments`, `result`, `parent_ids`, and `chargeable` (boolean). Explicit `cost_usd` must be a finite nonnegative estimate. An unknown cost for a chargeable event keeps total dollars unknown. Tools without charges should say `chargeable: false`.

## Separate grader

After the workflow exits, a new grader process receives `artifact`, `final_state`, `checks`, and `expected`. Its response contains boolean `passed`, boolean `unsafe`, and `checks`, a list of `{name, passed}`. Optional `execution_error` marks a grading/execution failure. A passed verdict cannot contain a failing check, unsafe outcome, or execution error. Grade actual state or produced behavior. Do not inspect a candidate ID to decide whether it should pass, edit a saved trace, or repair an artifact before scoring.

Errors produce failed runs with generic process errors, rather than copying provider diagnostics. Cancellation and global budget stops pause the experiment without scoring unfinished jobs. Adapters execute trusted local code with inherited access needed by their workflow; this protocol is not a sandbox for hostile third-party code.

## Bundled independent workflows

In addition to the original [game-building crew](../integrations/crewai-game-builder/README.md), Alpha includes a pinned [language-handoff example](../integrations/swarm-handoff/README.md) and a pinned [writing-and-review loop](../integrations/crewai-self-review/README.md). Their README files document original control flow, exact adaptations, private rubrics, and limitations. The bundled tasks remain Alpha-authored development suites. Adding an independent workflow does not make its authored tasks an independent benchmark.
