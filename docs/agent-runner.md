# Executable agent orchestration

Alpha has a real local task runner alongside its earlier state-machine demo. The
runner makes **actual model requests**, persists tasks in SQLite, and can resume
work after a process exits. It is separate from the optimizer's experiment
controller, which has not yet been migrated to this scheduler.

## Use it

Install the existing Alpha CLI and authenticate the Codex CLI first.

```sh
python3 -m pip install -e .
alpha orch run "Compare two approaches to rate limiting and recommend one" \
  --db runs/limits.sqlite --workers 2
alpha orch status --db runs/limits.sqlite --verbose
alpha orch resume --db runs/limits.sqlite --workers 3
```

The first command prints the root's final result. **These are real model calls
and may incur usage.** No new auth mechanism is added: Codex uses your existing
CLI sign-in. The run creates the database once; use `resume` for a previously
started run, not a second `run` command with the same file.

Specify identities and skills with a JSON file:

```json
{
  "identities": {
    "lead": {
      "instructions": "Synthesize the answer. Delegate specialized questions only when useful.",
      "skills": ["skills/lead.md"],
      "can_delegate": true
    },
    "researcher": {
      "instructions": "Analyze the assigned problem and report evidence or uncertainty.",
      "skills": [],
      "can_delegate": false
    }
  }
}
```

```sh
alpha orch run "Analyze the design" --agents agents.json --identity lead \
  --db runs/design.sqlite --workers 2 --tiers small,large \
  --model-small gpt-5.1 --model-large gpt-5.1
```

Skill file **contents** are loaded when the run is created and saved with the
identity configuration. A worker chooses the identity per task, not permanently
per process. `--model-small`, `--model-medium`, and `--model-large` allow
model assignments; when omitted, the Codex CLI default is used regardless of
size. `--tiers` configures worker capacities, not actual hardware capacity.
Task size uses deterministic token/tool/file metadata rules from the scheduler;
large tasks get priority over small tasks when queued. Do not configure only
small workers if agents can delegate medium or large tasks.

## How parent suspension works

1. An idle worker transactionally claims a queued task and records a unique
   lease token. Concurrent workers cannot claim the same queued task.
2. The actual model returns a structured `complete` or `delegate` response.
3. On `delegate`, one SQLite transaction validates all children, creates their
   queue records, changes the parent to `waiting`, and **releases its slot**.
4. Any free worker can pick up children. When all children have completed, the
   database queues the parent. Its next model request includes the child results.
5. The resumed parent completes or delegates again, subject to depth, fanout,
   total-task, and delegation-round limits.

This prevents *worker-pool starvation from waiting parents* within this runner.
Parent/child dependencies are a tree, so the runner does not expose arbitrary
dependency edges that could form circular dependencies. It is not a general DAG
scheduler.

Claims are fenced by a lease token: a late or duplicated result from a worker
whose lease expired cannot overwrite the current owner. Heartbeats extend
leases during model calls. A crash leaves the task running until its lease
expires; another worker can then claim it. Automatic retries are safe here
because **agent model calls run read-only, with external tools forbidden**.
Duplicate model calls (and additional token cost) can still occur on crashes.

## Run your own agent program

You can run **real external agent code**, including programs that use tools,
instead of the Codex read-only model transport. Supply the command explicitly:

```sh
alpha orch run "Inspect the repository and report a fix" \
  --command "python3 my_agent.py" --db runs/code-task.sqlite --workers 2
```

Alpha starts one command per claimed task. The command reads one JSON object
from stdin with these fields:

```json
{
  "task_id": "stable task ID",
  "operation_id": "same stable ID for idempotency at your external service",
  "attempt": 1,
  "identity": "general",
  "prompt": "agent instructions, task, and completed child results",
  "schema": {"type": "object"}
}
```

`schema` is the complete response schema, abbreviated above. Your command
must write exactly one JSON object to stdout, for example:

```json
{"action":"complete","result":"I finished the requested work.","children":[]}
```

To delegate, return `{"action":"delegate","result":"","children":[...]}`,
where each child specifies `identity`, `prompt`, and `metadata` with integer
`expected_tokens`, `file_bytes`, and `tool_count`. The command is invoked
again for the parent after the children finish. Keep diagnostics on stderr,
not stdout. The command is trusted code supplied by the operator, and runs
with that user's local permissions and environment.

**Side effects:** An external command is assumed capable of changes by default.
Its claim is persisted before invocation. If it fails, times out, or its lease
expires, Alpha puts it in `needs_review` and blocks dependent tasks rather
than blindly repeating a potentially successful external operation.

Reconcile after checking the actual external state:

```sh
alpha orch status --db runs/code-task.sqlite --verbose
alpha orch resolve TASK_ID --db runs/code-task.sqlite --decision done --result "verified result"
# Or explicitly authorize a replay, after verifying the operation did not occur:
alpha orch resolve TASK_ID --db runs/code-task.sqlite --decision retry
alpha orch resume --db runs/code-task.sqlite
```

`--decision failed` permanently fails the uncertain task. For **genuinely
read-only** command adapters, you may specify `--read-only-command` when
creating the run to allow automatic recovery and bounded retries. Never use
that flag for code that writes files, calls mutating APIs, sends messages, or
may otherwise produce external effects.

The stable `operation_id` lets your worker propagate a deduplication key
to downstream APIs. Alpha **does not guarantee exactly once** for external
effects: the external system must enforce that key, or an operator must
reconcile uncertain outcomes. The initial command and policy are stored in
the run database and reused on resume.

## Optional dependency safety and budgets

The executable scheduler enforces configured limits transactionally as work is
created or dependencies are linked. Spawn depth, direct children, total tasks,
delegation rounds, and attempts retain their existing defaults. The following
additional controls are disabled unless explicitly requested:

- `--max-descendants N`: cap the number of descendants below **each ancestor**
  (across all generations, not only direct children).
- `--max-dependency-depth N`: cap the longest dependency path, including
  implicit parent-to-child waits. Dependency cycles are always rejected.
- `--allow-dependency-links`: permit operator-created links between existing
  tasks. Run `alpha orch depends TASK_ID --prerequisite OTHER_ID --db runs/job.sqlite`
  for queued or waiting tasks. Disabled by default.
- `--max-calls N`: count all admitted attempts, including retries.
- `--max-tokens N`: use reported input and output tokens.
- `--max-cost-usd USD --prices prices.json`: use reported tokens and your own
  per-million-token model prices.

Example `prices.json` (replace with real rates for the selected models):

```json
{
  "your-model": {"input": 1.0, "cached_input": 0.5, "output": 3.0}
}
```

For a USD cap, name a priced model for all task tiers using `--model` or the
three `--model-small`, `--model-medium`, and `--model-large` flags.
The configuration and prices are retained in the run DB across resumes.

Budget admission happens **before** a new task attempt is claimed. Already
running requests can exceed a token or USD ceiling, so these ceilings are
based on observed usage, not guarantees against additional provider charges.
Unknown usage pauses subsequent admissions under token/USD caps.

Codex reports token usage. External command adapters may provide an additional
`usage` response field with nonnegative `input_tokens`, `output_tokens`,
and optional `cached_input_tokens`. Adapters without usage reporting can use
`--max-calls`, but their first unknown usage will pause token/cost budgets.

View accumulated calls, tokens, estimated USD, and unknown attempts in
`alpha orch status --db runs/job.sqlite`. Budget-blocked tasks do not
silently restart on resume.

## Boundaries

- **Real:** Codex calls, multiple worker loops, SQLite state, worker slot
  release, child completion and parent resumption, identity/skill loading,
  bounded delegation, expiry/reclaim, token-fenced claims, restart/resume.
- **Command adapter:** executes operator-supplied worker processes with an
  explicit JSON protocol. Potential side effects are conservatively fenced
  for human reconciliation instead of silently replayed.
- **Not implemented:** connecting this scheduler to the existing experiment
  optimizer; remote multi-host infrastructure; an enforced external
  idempotency protocol; human approvals; process-level exactly-once execution.
- The existing `alpha orch demo` is **only** a scripted failure-mode example,
  not the implementation behind `alpha orch run`.

SQLite should be on local disk and is not a recommended multi-host queue.
Do not assume tasks with possible external side effects can be retried using
these read-only recovery rules.

## Tests

```sh
python3 -m unittest discover -s test -p '*_test.py' -v
```

`test/worker_runtime_test.py` uses a deterministic fake model at the
execution interface to verify actual scheduling and state recovery. It does
not validate the quality of real model delegation decisions.
