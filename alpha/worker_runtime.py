"""Persistent local agent orchestration with fenced claims and yielding parents.

This runner executes real Codex calls in a read-only sandbox. It does not
support mutating external tools or claim production distributed guarantees.
"""
from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
import sqlite3
import time
import uuid

from .backends import CodexBackend
from .processes import run_process
from .orchestration import classify, TIERS

RESPONSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["complete", "delegate"]},
        "result": {"type": "string"},
        "children": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "identity": {"type": "string"},
                "prompt": {"type": "string"},
                "metadata": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "expected_tokens": {"type": "integer"},
                        "tool_count": {"type": "integer"},
                        "file_bytes": {"type": "integer"},
                    }, "required": ["expected_tokens", "tool_count", "file_bytes"]},
            }, "required": ["identity", "prompt", "metadata"]}},
    }, "required": ["action", "result", "children"],
}


def load_identities(path: Path | None) -> dict:
    if path is None:
        return {"general": {"instructions": "Complete the assigned task accurately.",
                            "skills": [], "can_delegate": True}}
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("identities")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("Agent config requires a nonempty identities object.")
    result = {}
    for name, entry in entries.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            raise ValueError("Invalid agent identity.")
        instructions, skills = entry.get("instructions", ""), entry.get("skills", [])
        if not isinstance(instructions, str) or not isinstance(skills, list):
            raise ValueError("Agent instructions and skills must be text and a list.")
        if not all(isinstance(s, str) for s in skills):
            raise ValueError("Agent skills must be file paths.")
        if not isinstance(entry.get("can_delegate", False), bool):
            raise ValueError("can_delegate must be boolean.")
        result[name] = {
            "instructions": instructions,
            "skills": [{"name": p, "instructions": (path.parent / p).read_text(encoding="utf-8")}
                       for p in skills],
            "can_delegate": entry.get("can_delegate", False)}
    return result


class RunStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def transact(self, func):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            value = func(conn)
            conn.commit()
            return value
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start(self, prompt: str, identity: str, identities: dict, limits: dict, *,
              command: str | None = None, read_only_command: bool = False) -> str:
        if not prompt.strip() or identity not in identities:
            raise ValueError("Provide a task prompt and a defined agent identity.")
        if self.path.exists():
            raise ValueError("Run database already exists. Use orch resume instead.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, parent_id TEXT, identity TEXT NOT NULL,
                    prompt TEXT NOT NULL, metadata TEXT NOT NULL, tier INTEGER NOT NULL,
                    depth INTEGER NOT NULL, wave INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL, result TEXT, error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT, lease_token TEXT, lease_until REAL,
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX tasks_state_tier ON tasks(state,tier DESC,created);
                CREATE INDEX tasks_parent ON tasks(parent_id);
            """)
            root = uuid.uuid4().hex
            now = time.time()
            conn.execute("INSERT INTO settings VALUES (?,?)", (
                "config", json.dumps({"root": root, "identities": identities, "limits": limits,
                                       "command": command, "read_only": command is None or read_only_command})))
            conn.execute("""INSERT INTO tasks
                (id,parent_id,identity,prompt,metadata,tier,depth,state,created,updated)
                VALUES (?,NULL,?,?,?,0,0,'queued',?,?)""",
                (root, identity, prompt, "{}", now, now))
            return root
        finally:
            conn.close()

    def config(self) -> dict:
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='config'").fetchone()
            if row is None:
                raise ValueError("Not an Alpha orchestration database.")
            return json.loads(row[0])
        finally:
            conn.close()

    def claim(self, worker: str, tier: int, lease_seconds: float, attempts: int) -> dict | None:
        def tx(conn):
            now = time.time()
            settings = json.loads(conn.execute(
                "SELECT value FROM settings WHERE key='config'").fetchone()[0])
            if settings.get("read_only", True):
                expired = conn.execute("""SELECT id,parent_id FROM tasks WHERE
                    state='running' AND lease_until<=? AND attempts>=?""",
                    (now, attempts)).fetchall()
                conn.execute("""UPDATE tasks SET state='failed',error='Lease expired after final attempt',
                    lease_owner=NULL,lease_token=NULL,lease_until=NULL,updated=?
                    WHERE state='running' AND lease_until<=? AND attempts>=?""",
                    (now, now, attempts))
                conn.execute("""UPDATE tasks SET state='queued',lease_owner=NULL,
                    lease_token=NULL,lease_until=NULL,updated=?
                    WHERE state='running' AND lease_until<=?""", (now, now))
            else:
                expired = conn.execute("""SELECT id,parent_id FROM tasks WHERE
                    state='running' AND lease_until<=?""", (now,)).fetchall()
                # A mutating command may have succeeded before its worker disappeared.
                conn.execute("""UPDATE tasks SET state='needs_review',
                    error='Uncertain external side effects after expired lease',
                    lease_owner=NULL,lease_token=NULL,lease_until=NULL,updated=?
                    WHERE state='running' AND lease_until<=?""", (now, now))
            for item in expired:
                self._parents(conn, item["parent_id"], now)
            task = conn.execute("""SELECT * FROM tasks WHERE state='queued' AND tier<=?
                ORDER BY tier DESC,created,id LIMIT 1""", (tier,)).fetchone()
            if task is None:
                return None
            token = uuid.uuid4().hex
            conn.execute("""UPDATE tasks SET state='running',attempts=attempts+1,
                lease_owner=?,lease_token=?,lease_until=?,updated=? WHERE id=?""",
                (worker, token, now + lease_seconds, now, task["id"]))
            children = conn.execute("""SELECT identity,result FROM tasks WHERE parent_id=?
                ORDER BY created,id""", (task["id"],)).fetchall()
            return {**dict(task), "lease_token": token,
                    "children": [dict(item) for item in children]}
        return self.transact(tx)

    def heartbeat(self, task_id: str, token: str, seconds: float) -> bool:
        def tx(conn):
            now = time.time()
            return conn.execute("""UPDATE tasks SET lease_until=?,updated=?
                WHERE id=? AND state='running' AND lease_token=? AND lease_until>?""",
                (now + seconds, now, task_id, token, now)).rowcount == 1
        return self.transact(tx)

    def _parents(self, conn, parent_id: str | None, now: float):
        while parent_id:
            parent = conn.execute("SELECT id,parent_id,state FROM tasks WHERE id=?", (parent_id,)).fetchone()
            if not parent:
                return
            states = {r[0] for r in conn.execute("SELECT state FROM tasks WHERE parent_id=?", (parent_id,))}
            if parent["state"] in ("waiting", "blocked"):
                if states and states <= {"done"}:
                    conn.execute("UPDATE tasks SET state='queued',updated=? WHERE id=?", (now, parent_id))
                elif states & {"failed", "blocked", "needs_review"}:
                    conn.execute("UPDATE tasks SET state='blocked',updated=? WHERE id=?", (now, parent_id))
            parent_id = parent["parent_id"]

    def settle(self, task: dict, reply: dict, config: dict) -> bool:
        def tx(conn):
            now = time.time()
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
            if row["state"] != "running" or row["lease_token"] != task["lease_token"] or row["lease_until"] <= now:
                return False
            children = reply["children"]
            if reply["action"] == "delegate":
                limits = config["limits"]
                existing = conn.execute("SELECT count(*) FROM tasks WHERE parent_id=?", (row["id"],)).fetchone()[0]
                total = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
                if (not config["identities"][row["identity"]]["can_delegate"] or not children
                    or row["depth"] >= limits["max_depth"] or row["wave"] >= limits["max_rounds"]
                    or existing + len(children) > limits["max_children"]
                    or total + len(children) > limits["max_tasks"]):
                    raise ValueError("Delegation forbidden or task count/depth limit exceeded.")
                validated = []
                for child in children:
                    if (not isinstance(child, dict) or child.get("identity") not in config["identities"]
                        or not isinstance(child.get("prompt"), str) or not child["prompt"].strip()):
                        raise ValueError("Unknown identity or empty child prompt.")
                    metadata = child.get("metadata")
                    if not isinstance(metadata, dict):
                        raise ValueError("Invalid child metadata.")
                    size = classify(metadata)
                    validated.append((child, TIERS[size]))
                for child, tier in validated:
                    conn.execute("""INSERT INTO tasks
                        (id,parent_id,identity,prompt,metadata,tier,depth,state,created,updated)
                        VALUES (?,?,?,?,?,?,?,'queued',?,?)""", (
                        uuid.uuid4().hex, row["id"], child["identity"], child["prompt"],
                        json.dumps(child["metadata"]), tier, row["depth"] + 1, now, now))
                conn.execute("""UPDATE tasks SET state='waiting',wave=wave+1,updated=?,
                    lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE id=?""",
                    (now, row["id"]))
            elif reply["action"] == "complete":
                if children:
                    raise ValueError("Complete must not include child requests.")
                conn.execute("""UPDATE tasks SET state='done',result=?,error=NULL,updated=?,
                    lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE id=?""",
                    (json.dumps(reply["result"]), now, row["id"]))
                self._parents(conn, row["parent_id"], now)
            else:
                raise ValueError("Unknown agent action.")
            return True
        return self.transact(tx)

    def error(self, task: dict, message: str, attempts: int) -> bool:
        def tx(conn):
            now = time.time()
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
            if row["state"] != "running" or row["lease_token"] != task["lease_token"] or row["lease_until"] <= now:
                return False
            settings = json.loads(conn.execute(
                "SELECT value FROM settings WHERE key='config'").fetchone()[0])
            state = ("needs_review" if not settings.get("read_only", True)
                     else "queued" if row["attempts"] < attempts else "failed")
            conn.execute("""UPDATE tasks SET state=?,error=?,updated=?,
                lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE id=?""",
                (state, message[:800], now, row["id"]))
            if state in ("failed", "needs_review"):
                self._parents(conn, row["parent_id"], now)
            return True
        return self.transact(tx)

    def resolve(self, task_id: str, decision: str, result: str | None = None) -> None:
        """An operator confirms an external effect or explicitly authorizes replay."""
        if decision not in ("done", "failed", "retry"):
            raise ValueError("Decision must be done, failed, or retry.")
        if decision == "done" and result is None:
            raise ValueError("Completing an uncertain task requires a confirmed result.")
        def tx(conn):
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] != "needs_review":
                raise ValueError("Only needs_review tasks can be reconciled.")
            now = time.time()
            state = "queued" if decision == "retry" else decision
            attempts = 0 if decision == "retry" else row["attempts"]
            conn.execute("""UPDATE tasks SET state=?,result=?,error=NULL,attempts=?,
                lease_owner=NULL,lease_token=NULL,lease_until=NULL,updated=? WHERE id=?""",
                (state, json.dumps(result) if state == "done" else None, attempts, now, task_id))
            self._parents(conn, row["parent_id"], now)
        self.transact(tx)

    def status(self) -> dict:
        conn = self.connect()
        try:
            config = self.config()
            tasks = [dict(r) for r in conn.execute("""SELECT id,parent_id,identity,state,depth,
                wave,attempts,result,error FROM tasks ORDER BY depth,created,id""")]
            return {"root": config["root"], "tasks": tasks,
                    "counts": {state: sum(t["state"] == state for t in tasks)
                               for state in ("queued", "running", "waiting", "done", "failed", "blocked", "needs_review")}}
        finally:
            conn.close()

    def cannot_progress(self, highest_tier: int) -> bool:
        """Detect a queue no configured worker could ever service."""
        conn = self.connect()
        try:
            running = conn.execute("SELECT count(*) FROM tasks WHERE state='running'").fetchone()[0]
            eligible = conn.execute("""SELECT count(*) FROM tasks WHERE state='queued'
                AND tier<=?""", (highest_tier,)).fetchone()[0]
            pending = conn.execute("""SELECT count(*) FROM tasks WHERE state IN
                ('queued','waiting')""").fetchone()[0]
            return bool(pending and not running and not eligible)
        finally:
            conn.close()

    def finished(self) -> bool:
        conn = self.connect()
        try:
            return conn.execute("""SELECT count(*) FROM tasks WHERE
                state IN ('queued','running','waiting')""").fetchone()[0] == 0
        finally:
            conn.close()


def task_prompt(task: dict, config: dict) -> str:
    identity = config["identities"][task["identity"]]
    instructions = identity["instructions"] + "".join(
        f"\nSKILL {s['name']}\n{s['instructions']}" for s in identity["skills"])
    children = [{"identity": c["identity"], "result": json.loads(c["result"])}
                for c in task["children"] if c["result"] is not None]
    return (
        "Execute this task as a read-only agent. Do not call external tools, change files, "
        "or perform real-world operations. Return only schema-compliant JSON. "
        "Use action=complete, result=your answer, children=[] to finish. "
        "Use action=delegate, result='', children=[...] to create subagent tasks. "
        "You will release the worker slot while children execute and be resumed with their results. "
        "Do not repeat already-completed delegation. "
        f"Available identities: {json.dumps({k: {'can_delegate': v['can_delegate']} for k,v in config['identities'].items()})}. "
        f"Your identity: {task['identity']}; delegation allowed: {identity['can_delegate']}. "
        f"Depth: {task['depth']}; delegation rounds used: {task['wave']}.\n"
        f"ROLE INSTRUCTIONS\n{instructions}\nTASK\n{task['prompt']}\n"
        f"COMPLETED CHILD RESULTS\n{json.dumps(children, ensure_ascii=False)}")


async def execute(store: RunStore, task: dict, config: dict, model: str | None,
                  backend_factory, lease_seconds: float):
    stop = asyncio.Event()
    async def renew():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=lease_seconds / 3)
            except asyncio.TimeoutError:
                if not store.heartbeat(task["id"], task["lease_token"], lease_seconds):
                    stop.set()
    heartbeat = asyncio.create_task(renew())
    try:
        if config.get("command"):
            payload = {
                "task_id": task["id"], "operation_id": task["id"],
                "attempt": task["attempts"], "identity": task["identity"],
                "prompt": task_prompt(task, config), "schema": RESPONSE_SCHEMA,
            }
            stdout = await run_process(shlex.split(config["command"]),
                                       json.dumps(payload, ensure_ascii=False),
                                       timeout=300, label="Agent command")
            reply = json.loads(stdout)
        else:
            backend = backend_factory(model)
            response = await backend.complete(schema=RESPONSE_SCHEMA, prompt=task_prompt(task, config))
            reply = response["output"]
        if stop.is_set():
            return
        if not isinstance(reply, dict) or reply.get("action") not in ("complete", "delegate"):
            raise ValueError("Invalid agent action.")
        if not isinstance(reply.get("result"), str) or not isinstance(reply.get("children"), list):
            raise ValueError("Invalid agent response fields.")
        store.settle(task, reply, config)
    except Exception as exc:
        store.error(task, f"{type(exc).__name__}: {exc}", config["limits"]["max_attempts"])
    finally:
        stop.set()
        await heartbeat


async def run_workers(store: RunStore, *, workers: int = 2, tiers: list[str] | None = None,
                      models: dict | None = None, lease_seconds: float = 120,
                      backend_factory=None) -> dict:
    if workers < 1 or lease_seconds < 3:
        raise ValueError("Workers must be positive and lease at least 3 seconds.")
    tier_names = tiers or ["large"] * workers
    if len(tier_names) != workers or any(t not in TIERS for t in tier_names):
        raise ValueError("Provide exactly one valid size tier for each worker.")
    config = store.config()
    models = models or {}
    backend_factory = backend_factory or (lambda model: CodexBackend(model=model))
    async def worker(index):
        name = f"worker-{uuid.uuid4().hex[:12]}-{index}"
        while True:
            task = store.claim(name, TIERS[tier_names[index]], lease_seconds,
                               config["limits"]["max_attempts"])
            if task:
                size = next(k for k, v in TIERS.items() if v == task["tier"])
                await execute(store, task, config, models.get(size), backend_factory, lease_seconds)
            elif store.finished():
                return
            elif store.cannot_progress(max(TIERS[t] for t in tier_names)):
                raise ValueError("No configured worker can execute the remaining task tiers.")
            else:
                await asyncio.sleep(0.2)
    await asyncio.gather(*(worker(i) for i in range(workers)))
    return store.status()
