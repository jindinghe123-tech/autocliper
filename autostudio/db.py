"""SQLite storage for sessions, messages, scheduled tasks and task runs."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'chat',
    task_id TEXT,
    provider TEXT,
    model TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    cron TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    provider TEXT,
    model TEXT,
    allow_dangerous INTEGER NOT NULL DEFAULT 0,
    webhook TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_run_at REAL,
    last_status TEXT
);
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    session_id TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id, id);
"""

TASK_FIELDS = ("name", "prompt", "cron", "enabled", "provider", "model", "allow_dangerous", "webhook")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # ---- sessions -------------------------------------------------------
    def create_session(self, title: str = "新对话", kind: str = "chat", task_id: str | None = None,
                       provider: str | None = None, model: str | None = None) -> dict[str, Any]:
        sid = uuid.uuid4().hex[:12]
        now = time.time()
        self._exec(
            "INSERT INTO sessions (id, title, kind, task_id, provider, model, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, title, kind, task_id, provider, model, now, now),
        )
        return self.get_session(sid)  # type: ignore[return-value]

    def get_session(self, sid: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM sessions WHERE id=?", (sid,))

    def list_sessions(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,))

    def update_session(self, sid: str, **fields: Any) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("title", "provider", "model")}
        if not allowed:
            return
        sets = ", ".join(f"{k}=?" for k in allowed)
        self._exec(f"UPDATE sessions SET {sets}, updated_at=? WHERE id=?", (*allowed.values(), time.time(), sid))

    def touch_session(self, sid: str) -> None:
        self._exec("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), sid))

    def delete_session(self, sid: str) -> None:
        self._exec("DELETE FROM messages WHERE session_id=?", (sid,))
        self._exec("DELETE FROM sessions WHERE id=?", (sid,))

    # ---- messages -------------------------------------------------------
    def add_message(self, sid: str, data: dict[str, Any]) -> int:
        cur = self._exec(
            "INSERT INTO messages (session_id, data, created_at) VALUES (?,?,?)",
            (sid, json.dumps(data, ensure_ascii=False), time.time()),
        )
        self.touch_session(sid)
        return int(cur.lastrowid or 0)

    def get_messages(self, sid: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT id, data FROM messages WHERE session_id=? ORDER BY id", (sid,))
        out = []
        for r in rows:
            d = json.loads(r["data"])
            d["_id"] = r["id"]
            out.append(d)
        return out

    # ---- tasks ----------------------------------------------------------
    def create_task(self, **fields: Any) -> dict[str, Any]:
        tid = uuid.uuid4().hex[:10]
        now = time.time()
        vals = {k: fields.get(k) for k in TASK_FIELDS}
        vals["enabled"] = 1 if vals.get("enabled", True) in (True, 1, None) else 0
        vals["allow_dangerous"] = 1 if vals.get("allow_dangerous") else 0
        self._exec(
            f"INSERT INTO tasks (id, {', '.join(TASK_FIELDS)}, created_at, updated_at) VALUES (?, {', '.join('?' * len(TASK_FIELDS))}, ?, ?)",
            (tid, *[vals[k] for k in TASK_FIELDS], now, now),
        )
        return self.get_task(tid)  # type: ignore[return-value]

    def get_task(self, tid: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM tasks WHERE id=?", (tid,))

    def list_tasks(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM tasks ORDER BY created_at")

    def update_task(self, tid: str, **fields: Any) -> dict[str, Any] | None:
        vals = {k: v for k, v in fields.items() if k in TASK_FIELDS}
        for k in ("enabled", "allow_dangerous"):
            if k in vals:
                vals[k] = 1 if vals[k] else 0
        if vals:
            sets = ", ".join(f"{k}=?" for k in vals)
            self._exec(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?", (*vals.values(), time.time(), tid))
        return self.get_task(tid)

    def delete_task(self, tid: str) -> None:
        self._exec("DELETE FROM tasks WHERE id=?", (tid,))
        self._exec("DELETE FROM task_runs WHERE task_id=?", (tid,))

    def start_task_run(self, tid: str, session_id: str) -> int:
        cur = self._exec(
            "INSERT INTO task_runs (task_id, session_id, started_at, status) VALUES (?,?,?,?)",
            (tid, session_id, time.time(), "running"),
        )
        self._exec("UPDATE tasks SET last_run_at=?, last_status=? WHERE id=?", (time.time(), "running", tid))
        return int(cur.lastrowid or 0)

    def finish_task_run(self, run_id: int, tid: str, status: str, summary: str) -> None:
        self._exec(
            "UPDATE task_runs SET finished_at=?, status=?, summary=? WHERE id=?",
            (time.time(), status, summary, run_id),
        )
        self._exec("UPDATE tasks SET last_status=? WHERE id=?", (status, tid))

    def list_task_runs(self, tid: str, limit: int = 30) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT ?", (tid, limit))
