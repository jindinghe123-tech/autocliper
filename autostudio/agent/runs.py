"""In-flight agent runs: event buffering, live subscribers, cancellation and tool approvals."""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any, AsyncIterator, Awaitable, Callable

log = logging.getLogger("autostudio.runs")


class Run:
    def __init__(self, session_id: str):
        self.session_id = session_id
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

    async def request_approval(self, approval_id: str, name: str, args: dict[str, Any], timeout: float = 900) -> bool:
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.approvals[approval_id] = fut
        await self.emit({"type": "approval_request", "id": approval_id, "name": name, "arguments": args})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self.approvals.pop(approval_id, None)
            await self.emit({"type": "approval_resolved", "id": approval_id})


class RunManager:
    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}

    def get(self, sid: str) -> Run | None:
        return self.runs.get(sid)

    def is_running(self, sid: str) -> bool:
        r = self.runs.get(sid)
        return bool(r and not r.done)

    def start(self, sid: str, fn: Callable[[Run], Awaitable[Any]]) -> Run:
        if self.is_running(sid):
            raise RuntimeError("该对话正在运行中")
        run = Run(sid)
        self.runs[sid] = run

        async def wrapper() -> None:
            try:
                await fn(run)
            except asyncio.CancelledError:
                await run.emit({"type": "cancelled"})
            except Exception as e:  # noqa: BLE001
                log.error("run failed: %s", traceback.format_exc())
                await run.emit({"type": "error", "message": str(e) or type(e).__name__})
            finally:
                run.done = True
                await run.emit({"type": "done"})
                for q in list(run.subscribers):
                    q.put_nowait(None)
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

    def resolve_approval(self, sid: str, approval_id: str, approved: bool) -> bool:
        r = self.runs.get(sid)
        fut = r.approvals.get(approval_id) if r else None
        if not fut or fut.done():
            return False
        fut.set_result(approved)
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
