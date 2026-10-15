"""In-flight agent runs: event buffering, live subscribers, cancellation and tool approvals."""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any, AsyncIterator, Awaitable, Callable

log = logging.getLogger("autostudio.runs")


Notify = Callable[[str, dict[str, Any]], None]


class Run:
    def __init__(self, session_id: str, notify: Notify | None = None):
        self.session_id = session_id
        self.notify = notify        # hears what a phone should be told about (see remote.Notices)
        self.events: list[dict[str, Any]] = []
        self.subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self.done = False
        self.task: asyncio.Task[None] | None = None
        self.approvals: dict[str, asyncio.Future[bool]] = {}
        self.started = time.time()

    async def emit(self, ev: dict[str, Any]) -> None:
        ev = {**ev, "seq": len(self.events)}
        self.events.append(ev)
        for q in list(self.subscribers):
            q.put_nowait(ev)

    def notice(self, ev: dict[str, Any]) -> None:
        if not self.notify:
            return
        try:
            self.notify(self.session_id, {**ev, "seconds": int(time.time() - self.started)})
        except Exception:  # noqa: BLE001 — a notification must never break the run
            log.warning("run notice failed: %s", traceback.format_exc())

    async def request_approval(self, approval_id: str, name: str, args: dict[str, Any], timeout: float = 900,
                               can_remember: bool = False) -> bool | str:
        """True / False, or "session" when the user allowed this kind of action for the rest of the conversation."""
        fut: asyncio.Future[bool | str] = asyncio.get_running_loop().create_future()
        self.approvals[approval_id] = fut
        await self.emit({"type": "approval_request", "id": approval_id, "name": name, "arguments": args,
                         "can_remember": can_remember})
        self.notice({"type": "approval_needed", "approval_id": approval_id, "name": name})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self.approvals.pop(approval_id, None)
            await self.emit({"type": "approval_resolved", "id": approval_id})
            self.notice({"type": "approval_resolved", "approval_id": approval_id})


class RunManager:
    def __init__(self, notify: Notify | None = None) -> None:
        self.notify = notify
        self.runs: dict[str, Run] = {}
        self.session_allow: dict[str, set[str]] = {}   # session id -> permission kinds allowed for the conversation

    def get(self, sid: str) -> Run | None:
        return self.runs.get(sid)

    def is_running(self, sid: str) -> bool:
        r = self.runs.get(sid)
        return bool(r and not r.done)

    def start(self, sid: str, fn: Callable[[Run], Awaitable[Any]]) -> Run:
        if self.is_running(sid):
            raise RuntimeError("该对话正在运行中")
        run = Run(sid, self.notify)
        self.runs[sid] = run

        async def wrapper() -> None:
            notice: dict[str, Any] | None = None     # stopping a run yourself needs no notification
            try:
                out = await fn(run)
                notice = {"type": "run_done", "text": out if isinstance(out, str) else ""}
            except asyncio.CancelledError:
                await run.emit({"type": "cancelled"})
            except Exception as e:  # noqa: BLE001
                log.error("run failed: %s", traceback.format_exc())
                await run.emit({"type": "error", "message": str(e) or type(e).__name__})
                notice = {"type": "run_error", "text": str(e) or type(e).__name__}
            finally:
                run.done = True
                await run.emit({"type": "done"})
                for q in list(run.subscribers):
                    q.put_nowait(None)
                if notice:
                    run.notice(notice)
                asyncio.get_running_loop().call_later(120, self._cleanup, sid, run)

        run.task = asyncio.create_task(wrapper())
        return run

    def _cleanup(self, sid: str, run: Run) -> None:
        if self.runs.get(sid) is run and run.done:
            self.runs.pop(sid, None)

    async def wait(self, sid: str) -> None:
        r = self.runs.get(sid)
        if r and r.task:
            await asyncio.shield(r.task)

    def stop(self, sid: str) -> bool:
        r = self.runs.get(sid)
        if not r or r.done or not r.task:
            return False
        for fut in r.approvals.values():
            if not fut.done():
                fut.set_result(False)
        r.task.cancel()
        return True

    def resolve_approval(self, sid: str, approval_id: str, approved: bool, remember: bool = False) -> bool:
        r = self.runs.get(sid)
        fut = r.approvals.get(approval_id) if r else None
        if not fut or fut.done():
            return False
        fut.set_result("session" if approved and remember else approved)
        return True

    async def subscribe(self, sid: str, after: int = -1) -> AsyncIterator[dict[str, Any]]:
        r = self.runs.get(sid)
        if not r:
            yield {"type": "idle"}
            return
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        backlog = [e for e in r.events if e["seq"] > after]
        r.subscribers.add(q)
        try:
            for e in backlog:
                yield e
            if r.done:
                return
            last = backlog[-1]["seq"] if backlog else after
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"type": "ping"}
                    continue
                if ev is None:
                    return
                if ev["seq"] <= last:
                    continue
                last = ev["seq"]
                yield ev
        finally:
            r.subscribers.discard(q)
