"""SQLite storage for sessions, messages, scheduled tasks, task runs and social publishing."""
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
CREATE TABLE IF NOT EXISTS social_accounts (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok',
    profile TEXT,
    secret TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS social_posts (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT NOT NULL,
    url TEXT,
    error TEXT,
    note TEXT,
    session_id TEXT,
    task_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON social_posts(status, created_at);
"""

# Columns added after the first release: (table, column, definition)
MIGRATIONS = [("tasks", "auto_publish", "INTEGER NOT NULL DEFAULT 0")]

TASK_FIELDS = ("name", "prompt", "cron", "enabled", "provider", "model", "allow_dangerous", "webhook", "auto_publish")
TASK_BOOLS = ("enabled", "allow_dangerous", "auto_publish")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            for table, col, ddl in MIGRATIONS:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
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
        vals["auto_publish"] = 1 if vals.get("auto_publish") else 0
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
        for k in TASK_BOOLS:
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

    # ---- social accounts ------------------------------------------------
    # `profile` is public JSON (channel name, avatar…); `secret` holds tokens and never leaves the server.
    def create_account(self, platform: str, name: str, profile: dict[str, Any] | None = None,
                       secret: dict[str, Any] | None = None, account_id: str | None = None) -> dict[str, Any]:
        aid = account_id or uuid.uuid4().hex[:10]
        now = time.time()
        self._exec(
            "INSERT INTO social_accounts (id, platform, name, status, profile, secret, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (aid, platform, name, "ok", json.dumps(profile or {}, ensure_ascii=False),
             json.dumps(secret or {}, ensure_ascii=False), now, now),
        )
        return self.get_account(aid)  # type: ignore[return-value]

    @staticmethod
    def _account(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row:
            row["profile"] = json.loads(row.get("profile") or "{}")
            row["secret"] = json.loads(row.get("secret") or "{}")
        return row

    def get_account(self, aid: str) -> dict[str, Any] | None:
        return self._account(self._one("SELECT * FROM social_accounts WHERE id=?", (aid,)))

    def list_accounts(self, platform: str | None = None) -> list[dict[str, Any]]:
        if platform:
            rows = self._all("SELECT * FROM social_accounts WHERE platform=? ORDER BY created_at", (platform,))
        else:
            rows = self._all("SELECT * FROM social_accounts ORDER BY platform, created_at")
        return [self._account(r) for r in rows]  # type: ignore[misc]

    def update_account(self, aid: str, **fields: Any) -> dict[str, Any] | None:
        vals: dict[str, Any] = {}
        for k, v in fields.items():
            if k in ("name", "status"):
                vals[k] = v
            elif k in ("profile", "secret"):
                vals[k] = json.dumps(v or {}, ensure_ascii=False)
        if vals:
            sets = ", ".join(f"{k}=?" for k in vals)
            self._exec(f"UPDATE social_accounts SET {sets}, updated_at=? WHERE id=?", (*vals.values(), time.time(), aid))
        return self.get_account(aid)

    def delete_account(self, aid: str) -> None:
        self._exec("DELETE FROM social_accounts WHERE id=?", (aid,))

    # ---- social posts ---------------------------------------------------
    def create_post(self, account_id: str, platform: str, content: dict[str, Any], status: str = "pending",
                    session_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        pid = uuid.uuid4().hex[:10]
        now = time.time()
        self._exec(
            "INSERT INTO social_posts (id, account_id, platform, status, content, session_id, task_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, account_id, platform, status, json.dumps(content, ensure_ascii=False), session_id, task_id, now, now),
        )
        return self.get_post(pid)  # type: ignore[return-value]

    @staticmethod
    def _post(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row:
            row["content"] = json.loads(row.get("content") or "{}")
        return row

    def get_post(self, pid: str) -> dict[str, Any] | None:
        return self._post(self._one("SELECT * FROM social_posts WHERE id=?", (pid,)))

    def list_posts(self, status: str | None = None, limit: int = 100, session_id: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if session_id:
            where.append("session_id=?")
            args.append(session_id)
        sql = "SELECT * FROM social_posts" + (f" WHERE {' AND '.join(where)}" if where else "")
        rows = self._all(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit))
        return [self._post(r) for r in rows]  # type: ignore[misc]

    def update_post(self, pid: str, **fields: Any) -> dict[str, Any] | None:
        vals = {k: v for k, v in fields.items() if k in ("status", "url", "error", "note")}
        if "content" in fields:
            vals["content"] = json.dumps(fields["content"], ensure_ascii=False)
        if vals:
            sets = ", ".join(f"{k}=?" for k in vals)
            self._exec(f"UPDATE social_posts SET {sets}, updated_at=? WHERE id=?", (*vals.values(), time.time(), pid))
        return self.get_post(pid)

    def delete_post(self, pid: str) -> None:
        self._exec("DELETE FROM social_posts WHERE id=?", (pid,))
