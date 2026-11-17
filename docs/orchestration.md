# Subagent orchestration laboratory

Alpha now contains a deterministic in-memory simulator for subagent scheduling. It makes no model or provider calls and does not change Alpha's existing experiments or their optimization decisions.

## Ideas and implemented behaviors

| Topic | Failure mode | Implemented demonstration |
| --- | --- | --- |
| Parent/child dependencies | A parent may lose a child result or never resume | Parents retain child IDs; child completion queues a continuation |
| Dependency cycles | A waits for B, B for C, C for A | Reject a dependency that creates a cycle |
| Worker starvation | Parents waiting for children occupy all worker slots | Suspend waiting parents and release their workers |
| Recursive delegation | Unbounded subagent trees exhaust resources | Limit child count and delegation depth; validate children as one batch |
| Failure propagation | A failed child leaves its ancestors waiting | Propagate failed dependencies and support deadline expiry |
| Deterministic task sizing | Small jobs waste large workers, large jobs run on undersized workers | Size from input metadata; choose an eligible model and worker tier |
| Interchangeable worker identities | One agent permanently tied to a particular worker | Each claim dynamically loads the agent's skills and permitted tools |
| Side-effect protection | Blind retry may repeat a real-world write | Read-only allowlist, side-effect marker, no automatic replay of ambiguous writes |
| Idempotency/recovery | Delivery may repeat after crashes | Operation ID receipts, conservative escalation, simulated snapshot recovery |

## Two different deadlock cases

Worker starvation **does not require a cycle**. With two workers, both parent tasks can spawn children and wait. If the waiting parents hold the two slots, their children cannot start even though the dependency graph is valid. In Alpha, the parents yield their slots, the children complete, and the parents are queued to resume.

Separately, a cycle such as A → B → C → A is impossible with any number of workers. The simulator rejects the edge that would close the cycle.

## Deterministic routing

No LLM classifies task size. The example rules are:

| Size | Trigger (any) | Model tier |
| --- | --- | --- |
| Small | No larger trigger | economy |
| Medium | 3,500+ expected tokens, 100 KB+ files, or 4+ expected tool calls | balanced |
| Large | 12,000+ expected tokens, 2 MB+ files, 9+ tools, or GPU required | capable |

The scheduler orders ready work from largest to smallest and chooses the smallest compatible idle worker. Special capabilities such as GPU must be explicitly available on that worker. These thresholds and model labels are illustrative, not tuned to real workloads.

Agent identity is separate from the worker and model tier: the identity owns skills and tool permissions; the worker supplies execution capacity. Any compatible free worker can load the requested identity when claiming work.

## Side effects and retries

The simulator uses a conservative side-effect rule. The runner distinguishes tools on a declared read-only allowlist from potentially mutating tools. Unknown tools default to potentially side-effecting; the marker is recorded before execution. If a potentially mutating operation has an uncertain result or the worker crashes, do not automatically replay it. Escalate it for reconciliation. Safely failed read-only work can retry, up to a bounded attempt limit.

Operation IDs can deduplicate a previously confirmed action within the simulator; uncertain operations still require manual review. This is **not** proof of exactly-once behavior: real persistence, provider idempotency, leases, and atomic transactions remain outside this demonstration.

## Commands

    alpha orch demo
    alpha orch demo --scenario starvation
    alpha orch demo --scenario cycles
    alpha orch demo --scenario routing
    alpha orch demo --scenario side-effects
    alpha orch demo --scenario skills
    alpha orch demo --scenario limits
    alpha orch demo --scenario starvation --verbose --out runs/subagent-lab.json

Standard output summarizes the simulation; verbose mode includes state transitions. The --out option saves the full JSON evidence. The simulator never executes a model or an external tool.

See alpha/orchestration.py for the implementation and test/orchestration_test.py for unit tests.

## Limitations

This is a **scripted orchestration simulation**, not a production distributed scheduler or a real Alpha agent-fleet benchmark. It does not schedule live model calls through Alpha's experiment controller, prove durability across machine crashes, provide distributed worker leases, or meter production usage. Integrating real agents requires an adapter that records dependencies and side effects and exposes scheduling policies to Alpha's normal experiment evaluation.
