"""Deterministic subagent orchestration laboratory.

This is an opt-in in-memory simulator, not a production distributed scheduler.
It demonstrates bounded delegation, dependency-cycle checks, releasing worker
slots while awaiting children, generic workers loading agent identities/skills,
deterministic workload-to-capacity routing, and conservative side-effect fencing.
No external tool actually executes; failure and recovery are injected events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from typing import Any

TIERS = {"small": 0, "medium": 1, "large": 2}
MODELS = {"small": "economy", "medium": "balanced", "large": "capable"}
TERMINAL = frozenset({"done", "failed", "blocked", "needs_review"})


@dataclass(frozen=True)
class AgentIdentity:
    name: str
    skills: tuple[str, ...]
    tools: tuple[str, ...]


@dataclass(frozen=True)
class Worker:
    name: str
    tier: str
    capabilities: tuple[str, ...] = ()


@dataclass
class Task:
    id: str
    identity: str
    metadata: dict[str, Any]
    depth: int = 0
    parent: str | None = None
    prerequisites: set[str] = field(default_factory=set)
    children: set[str] = field(default_factory=set)
    state: str = "queued"
    worker: str | None = None
    complexity: str = "small"
    model: str = "economy"
    attempts: int = 0
    side_effect_possible: bool = False
    execution_uncertain: bool = False
    result: Any = None


def classify(metadata: dict[str, Any]) -> str:
    """Rule-based sizing from caller-visible task metadata, not an LLM."""
    if not isinstance(metadata, dict):
        raise ValueError("Task metadata must be an object.")
    keys = ("expected_tokens", "file_bytes", "tool_count")
    if any(type(metadata.get(k, 0)) is not int or metadata.get(k, 0) < 0 for k in keys):
        raise ValueError("Complexity metadata must have nonnegative integer values.")
    if not isinstance(metadata.get("requires", []), list) or any(
        not isinstance(c, str) or not c for c in metadata.get("requires", [])
    ):
        raise ValueError("Required capabilities must be a list of nonempty names.")
    tokens = metadata.get("expected_tokens", 0)
    byte_count = metadata.get("file_bytes", 0)
    tools = metadata.get("tool_count", 0)
    if tokens >= 12000 or byte_count >= 2_000_000 or tools >= 9 or "gpu" in metadata.get("requires", []):
        return "large"
    if tokens >= 3500 or byte_count >= 100_000 or tools >= 4:
        return "medium"
    return "small"


class Scheduler:
    """Central, deterministic state-machine model (single-writer simulation)."""

    def __init__(
        self, workers: list[Worker], identities: list[AgentIdentity], *,
        readonly_tools: tuple[str, ...] = ("read", "search"),
        max_depth: int = 3, max_children: int = 4,
        max_attempts: int = 2,
    ) -> None:
        if not workers or not identities:
            raise ValueError("At least one worker and one identity are required.")
        if any(w.tier not in TIERS for w in workers) or len({w.name for w in workers}) != len(workers):
            raise ValueError("Workers require unique names and valid capacity tiers.")
        if len({a.name for a in identities}) != len(identities):
            raise ValueError("Agent identities must have unique names.")
        if any(not a.name or not a.skills or not a.tools for a in identities):
            raise ValueError("Agent identities need a name, skills, and permitted tools.")
        if any(type(x) is not int or x < 1 for x in (max_depth, max_children, max_attempts)):
            raise ValueError("Depth, fanout, and retry limits must be positive integers.")
        self.workers = {w.name: w for w in workers}
        self.identities = {a.name: a for a in identities}
        self.readonly_tools = frozenset(readonly_tools)
        self.max_depth, self.max_children, self.max_attempts = max_depth, max_children, max_attempts
        self.tasks: dict[str, Task] = {}
        self.events: list[dict] = []
        # Operation IDs represent completed or uncertain provider-visible effects.
        self.effect_ledger: dict[str, dict] = {}

    def _log(self, action: str, task: str | None = None, **detail: Any) -> None:
        self.events.append({"action": action, "task": task, **deepcopy(detail)})

    def submit(self, task_id: str, identity: str, metadata: dict | None = None) -> Task:
        if not isinstance(task_id, str) or not task_id or task_id in self.tasks:
            raise ValueError("Task IDs must be unique nonempty strings.")
        if identity not in self.identities:
            raise ValueError("Unknown agent identity.")
        data = deepcopy(metadata or {})
        size = classify(data)
        task = Task(id=task_id, identity=identity, metadata=data, complexity=size, model=MODELS[size])
        self.tasks[task_id] = task
        self._log("submitted", task_id, identity=identity, size=size, model=task.model)
        return task

    def _reaches(self, start: str, goal: str) -> bool:
        pending, visited = [start], set()
        while pending:
            item = pending.pop()
            if item == goal:
                return True
            if item not in visited:
                visited.add(item)
                pending.extend(self.tasks[item].prerequisites)
        return False

    def depends(self, task_id: str, prerequisite: str) -> None:
        if task_id not in self.tasks or prerequisite not in self.tasks:
            raise ValueError("Dependencies must refer to existing tasks.")
        task = self.tasks[task_id]
        if task.state not in ("queued", "waiting"):
            raise ValueError("Cannot mutate dependencies of running or terminal work.")
        if task_id == prerequisite or self._reaches(prerequisite, task_id):
            self._log("cycle_rejected", task_id, prerequisite=prerequisite)
            raise ValueError(f"Dependency cycle detected: {task_id} -> {prerequisite}.")
        task.prerequisites.add(prerequisite)
        self._log("dependency_added", task_id, prerequisite=prerequisite)

    def delegate(self, parent_id: str, children: list[dict]) -> list[Task]:
        """Atomically enqueue children, then suspend the parent and free its slot."""
        parent = self.tasks[parent_id]
        if parent.state != "running":
            raise ValueError("Only a running parent can delegate.")
        if "delegate" not in self.identities[parent.identity].tools:
            raise PermissionError("This agent identity cannot delegate.")
        if not children or len(parent.children) + len(children) > self.max_children:
            raise ValueError("Delegation requires children within the fanout limit.")
        if parent.depth >= self.max_depth:
            raise ValueError("Delegation depth exceeded.")
        ids = [row.get("id") for row in children]
        if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise ValueError("Delegated tasks must have unique nonempty IDs.")
        if any(i in self.tasks for i in ids):
            raise ValueError("Delegated task ID already exists.")
        # Validate the entire batch before changing any state.
        plans = []
        for row in children:
            identity = row.get("identity")
            if identity not in self.identities:
                raise ValueError("Unknown child identity.")
            metadata = deepcopy(row.get("metadata") or {})
            size = classify(metadata)
            plans.append((row["id"], identity, metadata, size))
        created = []
        for task_id, identity, metadata, size in plans:
            child = self.submit(task_id, identity, metadata)
            child.parent = parent_id
            child.depth = parent.depth + 1
            parent.children.add(task_id)
            parent.prerequisites.add(task_id)
            created.append(child)
        self._free(parent)
        parent.state = "waiting"
        self._log("parent_suspended", parent_id, children=ids, worker_released=True)
        return created

    def _free(self, task: Task) -> None:
        task.worker = None

    def dispatch(self) -> list[dict]:
        """Largest ready tasks first; smallest capable *idle* worker per task."""
        busy = {t.worker for t in self.tasks.values() if t.state == "running"}
        available = [w for w in self.workers.values() if w.name not in busy]
        ready = sorted(
            (t for t in self.tasks.values()
             if t.state == "queued"
             and all(self.tasks[d].state == "done" for d in t.prerequisites)),
            key=lambda t: (-TIERS[t.complexity], t.id),
        )
        assignments = []
        for task in ready:
            required = set(task.metadata.get("requires", []))
            eligible = sorted(
                (w for w in available if TIERS[w.tier] >= TIERS[task.complexity]
                 and required <= set(w.capabilities)),
                key=lambda w: (TIERS[w.tier], w.name),
            )
            if not eligible:
                continue
            worker = eligible[0]
            available.remove(worker)
            task.worker, task.state = worker.name, "running"
            task.attempts += 1
            identity = self.identities[task.identity]
            assignment = {
                "task": task.id, "worker": worker.name, "worker_tier": worker.tier,
                "identity": identity.name, "loaded_skills": list(identity.skills),
                "permitted_tools": list(identity.tools), "model": task.model,
                "attempt": task.attempts,
            }
            self._log("assigned", task.id, **{k: v for k, v in assignment.items() if k != "task"})
            assignments.append(assignment)
        return assignments

    def tool_call(self, task_id: str, tool: str, *,
                  operation_id: str | None = None, outcome: str = "ok") -> str:
        """Simulate a tool call: unknown tools are potentially mutating.

        A marker is persisted *before* an unsafe call; an uncertain outcome is
        never treated as proof the external operation didn't happen.
        """
        task = self.tasks[task_id]
        if task.state != "running":
            raise ValueError("Only running tasks may invoke tools.")
        if tool not in self.identities[task.identity].tools:
            raise PermissionError("Tool is not allowed for this agent identity.")
        if outcome not in ("ok", "uncertain"):
            raise ValueError("Tool outcome must be ok or uncertain.")
        safe = tool in self.readonly_tools
        if not safe and not operation_id:
            raise ValueError("Mutating or unknown tools require a stable operation_id.")
        if operation_id:
            previous = self.effect_ledger.get(operation_id)
            if previous is not None:
                if previous["tool"] != tool:
                    raise ValueError("Idempotency key reused for a different operation.")
                if previous["outcome"] == "uncertain":
                    task.execution_uncertain = True
                    self._log("effect_reconciliation_required", task_id, operation_id=operation_id)
                    return "needs_review"
                self._log("effect_deduplicated", task_id, operation_id=operation_id)
                return "deduplicated"
        if not safe:
            task.side_effect_possible = True
            self._log("side_effect_flag", task_id, tool=tool, operation_id=operation_id)
        if operation_id:
            self.effect_ledger[operation_id] = {"tool": tool, "outcome": outcome}
        if outcome == "uncertain":
            task.execution_uncertain = True
        self._log("tool_result", task_id, tool=tool, outcome=outcome)
        return outcome

    def _settle_dependents(self) -> None:
        """Wake satisfied parents and block chains containing failed dependencies."""
        changed = True
        while changed:
            changed = False
            for task in self.tasks.values():
                if task.state not in ("queued", "waiting", "blocked") or not task.prerequisites:
                    continue
                states = [self.tasks[d].state for d in task.prerequisites]
                if any(s in ("failed", "blocked", "needs_review") for s in states):
                    if task.state != "blocked":
                        task.state = "blocked"
                        self._log("dependent_blocked", task.id, prerequisites=sorted(task.prerequisites))
                        changed = True
                elif all(s == "done" for s in states) and task.state in ("waiting", "blocked"):
                    task.state = "queued"
                    self._log("parent_resume_queued", task.id, children=sorted(task.children))
                    changed = True
                elif task.state == "blocked":
                    # A safe retry or reconciliation has reopened a dependency.
                    task.state = "waiting"
                    self._log("dependent_waiting_again", task.id)
                    changed = True

    def finish(self, task_id: str, result: Any = None) -> None:
        task = self.tasks[task_id]
        if task.state != "running":
            raise ValueError("Only running tasks can finish.")
        if task.execution_uncertain:
            self.fail(task_id)
            return
        self._free(task)
        task.state, task.result = "done", deepcopy(result)
        self._log("completed", task_id)
        self._settle_dependents()

    def fail(self, task_id: str, *, uncertain: bool = False) -> None:
        task = self.tasks[task_id]
        if task.state != "running":
            raise ValueError("Only running tasks can fail.")
        task.execution_uncertain |= uncertain
        self._free(task)
        task.state = "needs_review" if task.side_effect_possible or task.execution_uncertain else "failed"
        self._log("failed", task_id, outcome=task.state, side_effect_possible=task.side_effect_possible)
        # Propagate dependent failures transitively, not just one generation.
        self._settle_dependents()

    def expire(self, task_id: str) -> None:
        """A deadline expires queued/waiting work instead of waiting forever."""
        task = self.tasks[task_id]
        if task.state == "running":
            # A timeout after invoking any unsafe tool cannot certify no effect.
            self.fail(task_id, uncertain=task.side_effect_possible)
            return
        if task.state not in ("queued", "waiting"):
            raise ValueError("Only pending work may expire.")
        task.state = "failed"
        self._log("deadline_expired", task_id)
        self._settle_dependents()

    def retry(self, task_id: str) -> bool:
        task = self.tasks[task_id]
        if task.state == "needs_review":
            self._log("retry_refused_effect", task_id)
            return False
        if task.state != "failed" or task.attempts >= self.max_attempts:
            return False
        task.state = "queued"
        self._log("retry_queued", task_id)
        self._settle_dependents()
        return True

    def resolve(self, task_id: str, *, completed: bool) -> None:
        """Human or external reconciler supplies proof; this doesn't replay."""
        task = self.tasks[task_id]
        if task.state != "needs_review":
            raise ValueError("Only uncertain side-effect work can be reconciled.")
        task.state = "done" if completed else "failed"
        self._log("manual_reconciliation", task_id, outcome=task.state)
        self._settle_dependents()

    def snapshot(self) -> dict:
        """JSON-serializable handoff state; no worker or provider credentials."""
        return {
            "workers": [{"name": w.name, "tier": w.tier,
                         "capabilities": list(w.capabilities)} for w in self.workers.values()],
            "identities": [{"name": i.name, "skills": list(i.skills),
                            "tools": list(i.tools)} for i in self.identities.values()],
            "readonly_tools": sorted(self.readonly_tools),
            "max_depth": self.max_depth, "max_children": self.max_children,
            "max_attempts": self.max_attempts,
            "tasks": [{
                **{k: deepcopy(getattr(t, k)) for k in (
                    "id", "identity", "metadata", "depth", "parent", "state",
                    "worker", "complexity", "model", "attempts",
                    "side_effect_possible", "execution_uncertain", "result",
                )},
                "prerequisites": sorted(t.prerequisites),
                "children": sorted(t.children),
            } for t in self.tasks.values()],
            "effects": deepcopy(self.effect_ledger), "events": deepcopy(self.events),
        }

    @classmethod
    def restore(cls, state: dict) -> "Scheduler":
        """Simulate a restart, requeueing safe work and fencing ambiguous work."""
        scheduler = cls(
            [Worker(w["name"], w["tier"], tuple(w["capabilities"])) for w in state["workers"]],
            [AgentIdentity(i["name"], tuple(i["skills"]), tuple(i["tools"])) for i in state["identities"]],
            readonly_tools=tuple(state["readonly_tools"]),
            max_depth=state["max_depth"], max_children=state["max_children"],
            max_attempts=state["max_attempts"],
        )
        for row in state["tasks"]:
            task = Task(**{k: deepcopy(v) for k, v in row.items() if k not in ("prerequisites", "children")})
            task.prerequisites = set(row["prerequisites"])
            task.children = set(row["children"])
            if task.state == "running":
                task.worker = None
                task.state = (
                    "needs_review" if task.side_effect_possible or task.execution_uncertain
                    else "queued" if task.attempts < scheduler.max_attempts else "failed"
                )
            scheduler.tasks[task.id] = task
        scheduler.effect_ledger = deepcopy(state["effects"])
        scheduler.events = deepcopy(state["events"])
        scheduler._log("restored", tasks=len(scheduler.tasks))
        scheduler._settle_dependents()
        return scheduler

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for task in self.tasks.values():
            counts[task.state] = counts.get(task.state, 0) + 1
        return {
            "states": counts, "running_workers": sorted(
                t.worker for t in self.tasks.values() if t.worker is not None
            ),
            "tasks": len(self.tasks), "events": len(self.events),
            "worker_capacity": len(self.workers),
        }


def demo(scenario: str = "all") -> dict:
    """Demonstrate independently testable behavior without model/provider calls."""
    valid = {"starvation", "cycles", "routing", "side-effects", "skills", "limits"}
    if scenario != "all" and scenario not in valid:
        raise ValueError(f"Unknown scenario: {scenario}.")
    results = {}
    def build(workers: list[Worker] | None = None, depth: int = 3) -> Scheduler:
        return Scheduler(
            workers or [Worker("w1", "large"), Worker("w2", "large")],
            [
                AgentIdentity("coordinator", ("planning",), ("read", "delegate", "write")),
                AgentIdentity("researcher", ("research", "lookup"), ("read", "search")),
            ],
            max_depth=depth,
        )

    if scenario in ("all", "starvation"):
        lab = build()
        lab.submit("parent-1", "coordinator")
        lab.submit("parent-2", "coordinator")
        lab.dispatch()
        lab.delegate("parent-1", [{"id": "child-1", "identity": "researcher"}])
        lab.delegate("parent-2", [{"id": "child-2", "identity": "researcher"}])
        released = lab.summary()["running_workers"] == []
        child_assignments = lab.dispatch()
        for child in ("child-1", "child-2"):
            lab.finish(child)
        resumed = lab.dispatch()
        for parent in ("parent-1", "parent-2"):
            lab.finish(parent)
        results["starvation"] = {
            "slots_released_while_waiting": released,
            "children_assigned": [x["task"] for x in child_assignments],
            "parents_resumed": [x["task"] for x in resumed],
            "summary": lab.summary(), "events": lab.events,
            "counterfactual": "If both parents retained their two slots, neither child could run.",
        }
    if scenario in ("all", "cycles"):
        lab = build()
        for item in ("A", "B", "C"):
            lab.submit(item, "coordinator")
        lab.depends("A", "B")
        lab.depends("B", "C")
        try:
            lab.depends("C", "A")
            rejected = False
        except ValueError:
            rejected = True
        results["cycles"] = {"cycle_rejected": rejected, "events": lab.events}
    if scenario in ("all", "routing"):
        lab = build([Worker("small-1", "small"), Worker("medium-1", "medium"), Worker("large-1", "large")])
        lab.submit("big", "coordinator", {"expected_tokens": 20000})
        lab.submit("medium", "researcher", {"expected_tokens": 5000})
        lab.submit("small", "coordinator", {"expected_tokens": 200})
        results["routing"] = {"assignments": lab.dispatch(), "events": lab.events}
    if scenario in ("all", "side-effects"):
        lab = build()
        lab.submit("send", "coordinator")
        lab.dispatch()
        lab.tool_call("send", "write", operation_id="op-send-1", outcome="uncertain")
        lab.fail("send")
        refused = not lab.retry("send")
        results["side-effects"] = {
            "blind_retry_refused": refused, "summary": lab.summary(),
            "events": lab.events,
        }
    if scenario in ("all", "skills"):
        lab = build([Worker("slot-1", "large"), Worker("slot-2", "large")])
        lab.submit("work-a", "researcher", {"expected_tokens": 100})
        first = lab.dispatch()
        lab.finish("work-a")
        lab.submit("work-b", "coordinator", {"expected_tokens": 100})
        second = lab.dispatch()
        results["skills"] = {"first": first, "second": second, "events": lab.events}
    if scenario in ("all", "limits"):
        lab = build(depth=1)
        lab.submit("root", "coordinator")
        lab.dispatch()
        lab.delegate("root", [{"id": "child", "identity": "coordinator"}])
        lab.dispatch()
        try:
            lab.delegate("child", [{"id": "grandchild", "identity": "researcher"}])
            depth_rejected = False
        except ValueError:
            depth_rejected = True
        results["limits"] = {"depth_rejected": depth_rejected, "events": lab.events}
    return {"mode": "deterministic_orchestration_simulation", "scenario": scenario,
            "not_a_live_agent_benchmark": True, "results": results}
