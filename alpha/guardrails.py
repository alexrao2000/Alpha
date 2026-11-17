"""Transactional policy checks for the live, persistent agent scheduler.

Dependency edges point from the task waiting to the prerequisite task.
Budgets stop *new* attempts based on reported usage; in-flight usage can overshoot.
"""
from __future__ import annotations

import math
import sqlite3
import time
from collections import defaultdict, deque

from .accounting import validate_prices


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS task_dependencies (
        task_id TEXT NOT NULL, depends_on_id TEXT NOT NULL,
        PRIMARY KEY(task_id, depends_on_id))""")
    conn.execute("""CREATE INDEX IF NOT EXISTS task_dependencies_target
        ON task_dependencies(depends_on_id)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS attempt_usage (
        lease_token TEXT PRIMARY KEY, task_id TEXT NOT NULL, model TEXT,
        state TEXT NOT NULL, input_tokens INTEGER, cached_input_tokens INTEGER,
        output_tokens INTEGER, estimated_cost_usd REAL, created REAL NOT NULL)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS attempt_usage_task ON attempt_usage(task_id)""")


def validate_limits(limits: dict) -> None:
    for name in ("max_depth", "max_children", "max_tasks", "max_rounds", "max_attempts",
                 "max_descendants", "max_dependency_depth", "max_calls", "max_tokens"):
        value = limits.get(name)
        if value is not None and (type(value) is not int or value < (0 if name == "max_descendants" else 1)):
            raise ValueError(f"{name} must be a positive integer (max_descendants permits zero).")
    usd = limits.get("max_cost_usd")
    if usd is not None and (type(usd) not in (int, float) or not math.isfinite(usd) or usd <= 0):
        raise ValueError("max_cost_usd must be a positive finite number.")
    if type(limits.get("allow_dependency_links", False)) is not bool:
        raise ValueError("allow_dependency_links must be boolean.")
    prices = validate_prices(limits.get("prices"))
    if usd is not None and not prices:
        raise ValueError("A cost budget requires a model pricing table.")


def graph_depth(conn: sqlite3.Connection, extra_edges=()) -> int:
    """Compute longest dependency chain iteratively and reject cycles."""
    graph = defaultdict(set)
    for task_id, _ in conn.execute("SELECT id,parent_id FROM tasks"):
        graph[task_id]
    def add(source, target):
        if source not in graph or target not in graph:
            raise ValueError("Dependency references an unknown task.")
        graph[source].add(target)
    for task_id, parent in conn.execute("SELECT id,parent_id FROM tasks"):
        if parent:
            add(parent, task_id)
    for source, target in conn.execute("SELECT task_id,depends_on_id FROM task_dependencies"):
        add(source, target)
    for source, target in extra_edges:
        add(source, target)
    indegree = {task_id: 0 for task_id in graph}
    for targets in graph.values():
        for target in targets:
            indegree[target] += 1
    pending = deque(task_id for task_id, degree in indegree.items() if degree == 0)
    longest = {task_id: 0 for task_id in graph}
    visited = 0
    while pending:
        task_id = pending.popleft()
        visited += 1
        for target in graph[task_id]:
            longest[target] = max(longest[target], longest[task_id] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    if visited != len(graph):
        raise ValueError("Dependency cycle detected.")
    return max(longest.values(), default=0)


def check_graph(conn: sqlite3.Connection, limit: int | None, extra_edges=()) -> None:
    depth = graph_depth(conn, extra_edges)
    if limit is not None and depth > limit:
        raise ValueError(f"Maximum dependency depth exceeded: {depth} > {limit}.")


def check_descendant_budget(conn: sqlite3.Connection, parent_id: str,
                            new_children: int, limit: int | None) -> None:
    """Limit descendants of every ancestor, not just the direct parent."""
    if limit is None:
        return
    ancestor = parent_id
    while ancestor:
        count = conn.execute("""WITH RECURSIVE descendants(id) AS (
            SELECT id FROM tasks WHERE parent_id=?
            UNION ALL
            SELECT t.id FROM tasks t JOIN descendants d ON t.parent_id=d.id
        ) SELECT count(*) FROM descendants""", (ancestor,)).fetchone()[0]
        if count + new_children > limit:
            raise ValueError(f"Maximum descendants exceeded for {ancestor}.")
        row = conn.execute("SELECT parent_id FROM tasks WHERE id=?", (ancestor,)).fetchone()
        ancestor = row[0] if row else None


def block_failed_dependencies(conn: sqlite3.Connection, now: float) -> None:
    """Propagate failed prerequisites to every queued/waiting dependent."""
    while True:
        changed = conn.execute("""UPDATE tasks SET state='blocked',
            error='Dependency failed or requires operator review',updated=?
            WHERE state IN ('queued','waiting') AND (
              EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on_id
                      WHERE d.task_id=tasks.id AND p.state IN ('failed','blocked','needs_review'))
              OR EXISTS (SELECT 1 FROM tasks child WHERE child.parent_id=tasks.id
                         AND child.state IN ('failed','blocked','needs_review')))""",
            (now,)).rowcount
        if not changed:
            break


def budget_snapshot(conn: sqlite3.Connection, limits: dict) -> dict:
    row = conn.execute("""SELECT count(*) AS calls,
        coalesce(sum(input_tokens+output_tokens),0) AS tokens,
        coalesce(sum(estimated_cost_usd),0) AS usd,
        coalesce(sum(CASE WHEN state='unknown' THEN 1 ELSE 0 END),0) AS unknown
        FROM attempt_usage""").fetchone()
    return {"calls": row[0], "reported_tokens": row[1],
            "estimated_cost_usd": row[2], "unknown_usage_attempts": row[3],
            "limits": {k: limits.get(k) for k in ("max_calls", "max_tokens", "max_cost_usd")}}


def budget_stop_reason(conn: sqlite3.Connection, limits: dict) -> str | None:
    if not any(limits.get(k) is not None for k in ("max_calls", "max_tokens", "max_cost_usd")):
        return None
    state = budget_snapshot(conn, limits)
    if limits.get("max_calls") is not None and state["calls"] >= limits["max_calls"]:
        return "Maximum model/command attempts reached"
    if limits.get("max_tokens") is not None or limits.get("max_cost_usd") is not None:
        if state["unknown_usage_attempts"]:
            return "Usage from an earlier attempt is unknown; budget enforcement paused"
        if limits.get("max_tokens") is not None and state["reported_tokens"] >= limits["max_tokens"]:
            return "Reported token budget reached"
        if limits.get("max_cost_usd") is not None and state["estimated_cost_usd"] >= limits["max_cost_usd"]:
            return "Estimated USD budget reached"
    return None


def start_attempt(conn: sqlite3.Connection, task_id: str, token: str, model: str | None) -> None:
    conn.execute("""INSERT INTO attempt_usage(lease_token,task_id,model,state,created)
        VALUES (?,?,?,'running',?)""", (token, task_id, model, time.time()))


def mark_unknown(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("UPDATE attempt_usage SET state='unknown' WHERE lease_token=? AND state='running'", (token,))


def report_usage(conn: sqlite3.Connection, token: str, usage: dict | None,
                 model: str | None, prices: dict, enforce_cost: bool) -> None:
    if usage is None:
        mark_unknown(conn, token)
        return
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                                         for k in ("input_tokens", "output_tokens")):
        raise ValueError("Usage must contain nonnegative integer input_tokens and output_tokens.")
    cache = usage.get("cached_input_tokens", 0)
    if type(cache) is not int or not 0 <= cache <= usage["input_tokens"]:
        raise ValueError("Invalid cached_input_tokens.")
    rates = prices.get(model or "")
    if enforce_cost and rates is None:
        raise ValueError(f"Missing price for model {model!r}; cannot enforce USD budget.")
    cost = None if rates is None else ((usage["input_tokens"] - cache) * rates["input"]
        + cache * rates["cached_input"] + usage["output_tokens"] * rates["output"]) / 1_000_000
    updated = conn.execute("""UPDATE attempt_usage SET state='reported',model=?,
        input_tokens=?,cached_input_tokens=?,output_tokens=?,estimated_cost_usd=?
        WHERE lease_token=? AND state IN ('running','unknown')""",
        (model, usage["input_tokens"], cache, usage["output_tokens"], cost, token)).rowcount
    if not updated:
        raise ValueError("Attempt usage already recorded or unknown lease token.")


def reopen_reconciled_dependencies(conn: sqlite3.Connection, now: float) -> None:
    """Reopen work blocked solely by dependencies that are now recoverable."""
    reason = "Dependency failed or requires operator review"
    while True:
        changed = conn.execute("""UPDATE tasks SET
            state=CASE WHEN EXISTS (SELECT 1 FROM tasks child WHERE child.parent_id=tasks.id
                                    AND child.state!='done') THEN 'waiting' ELSE 'queued' END,
            error=NULL,updated=?
            WHERE state='blocked' AND error=?
              AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on_id
                              WHERE d.task_id=tasks.id AND p.state IN ('failed','blocked','needs_review'))
              AND NOT EXISTS (SELECT 1 FROM tasks child WHERE child.parent_id=tasks.id
                              AND child.state IN ('failed','blocked','needs_review'))""",
            (now, reason)).rowcount
        if not changed:
            break
