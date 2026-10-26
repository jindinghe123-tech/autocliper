"""Cron-based scheduled agent tasks + result push to chat webhooks."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import state

log = logging.getLogger("autostudio.scheduler")

# ---------------------------------------------------------------------------
# material folder: every file is handed to a run once.
#
# Files are recognised by their content (a fingerprint of size + three slices), recorded in one ledger for
# every task (db.materials), and claimed the moment a run gets them. When the run is over they are used up
# if it succeeded or made something to publish, even if it crashed afterwards; otherwise they are tried
# again next time, up to MAX_ATTEMPTS. Used files move into 已用过/<date>/ inside the folder, so what is
# left in the folder is exactly what has not been made into anything yet.

WATCH_MAX = 30          # files handed to one run; the rest wait for the next one
WATCH_SETTLE_S = 60     # changed this recently: maybe still being copied in, so it waits for the next run
WATCH_SKIP = re.compile(r"^[.~]|\.(tmp|temp|part|partial|crdownload|download)$|^(thumbs\.db|desktop\.ini)$", re.I)
USED_DIR = "已用过"
MAX_ATTEMPTS = 3
FP_CHUNK = 1 << 20
CATCH_UP_S = 20 * 3600   # a run missed while the PC was off is made up for this long afterwards


def watch_dir_path(raw: str | None, workspace: Path) -> Path | None:
    """The folder a task watches, as an absolute path (relative ones live in the workspace)."""
    raw = str(raw or "").strip().strip('"').strip("'")
    if not raw:
        return None
    p = Path(os.path.expandvars(raw)).expanduser()
    return (p if p.is_absolute() else workspace / p).resolve()


def normalize_watch_dir(raw: str | None, workspace: Path) -> str | None:
    """Store the material folder as an absolute path, creating it so the user has somewhere to put files."""
    folder = watch_dir_path(raw, workspace)
    if not folder:
        return None
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"无法使用素材文件夹「{folder}」：{e}") from e
    return str(folder)


def _old_ledger(task: dict[str, Any], folder: Path) -> dict[str, str]:
    """What versions before the shared ledger remembered per task: {relative path: "size:mtime"}."""
    try:
        seen = json.loads(task.get("watch_seen") or "{}")
    except ValueError:
        return {}
    return (seen.get("files") or {}) if seen.get("dir") == str(folder) else {}


def fingerprint(path: Path, size: int) -> str:
    """Identifies a file by its content: the same video copied in again, or renamed, is the same file."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        if size <= 3 * FP_CHUNK:
            h.update(f.read())
        else:
            for at in (0, size // 2, size - FP_CHUNK):
                f.seek(at)
                h.update(f.read(FP_CHUNK))
    return f"{size}:{h.hexdigest()[:24]}"


def scan_folder(folder: Path, now: float | None = None) -> list[dict[str, Any]]:
    """Files in the material folder that are settled (not still being copied in), oldest first.

    Skips 已用过/ (material already made into something), hidden / temporary files and partial downloads.
    """
    now = time.time() if now is None else now
    out: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        top = Path(dirpath) == folder
        dirnames[:] = sorted(d for d in dirnames if not WATCH_SKIP.search(d) and not (top and d == USED_DIR))
        for name in filenames:
            if WATCH_SKIP.search(name):
                continue
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            if now - st.st_mtime < WATCH_SETTLE_S:
                continue
            out.append({"path": str(path), "rel": path.relative_to(folder).as_posix(), "size": st.st_size,
                        "mtime": st.st_mtime, "stat": f"{st.st_size}:{st.st_mtime_ns}"})
    out.sort(key=lambda f: (f["mtime"], f["rel"]))
    return out


def _fp(f: dict[str, Any]) -> str | None:
    known = state.db.material_by_stat(f["path"], f["stat"])
    if known:
        return known["fp"]
    try:
        return fingerprint(Path(f["path"]), f["size"])
    except OSError:     # locked or gone: try again next time
        return None


def move_used(folder: Path, path: Path, day: str | None = None) -> str | None:
    """Move a used file into 已用过/<date>/ (keeping its sub-folder); returns where it went."""
    try:
        rel = path.relative_to(folder)
    except ValueError:
        return None
    if not path.is_file():
        return None
    dest = folder / USED_DIR / (day or dt.date.today().isoformat()) / rel
    n = 2
    while dest.exists():
        dest = dest.with_name(f"{Path(rel.name).stem} ({n}){path.suffix}")
        n += 1
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
    except OSError as e:    # open in another program: the ledger still knows it is used
        log.info("could not move %s: %s", path, e)
        return None
    parent = path.parent
    while parent != folder and folder in parent.parents:      # tidy up sub-folders the move emptied
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return str(dest)


@dataclass
class Batch:
    """The material one run is given, and what to do with it when the run is over."""
    folder: Path
    files: list[dict[str, Any]] = field(default_factory=list)     # handed to the run (with "fp", "attempts")
    dupes: list[dict[str, Any]] = field(default_factory=list)     # second copies of those, moved along with them
    waiting: int = 0                                              # more new files than one run takes
    gave_up: list[dict[str, Any]] = field(default_factory=list)   # tried MAX_ATTEMPTS times already


def collect(task: dict[str, Any], folder: Path, running: Callable[[str], bool], now: float | None = None) -> Batch:
    """Work out which files in `folder` the next run of `task` gets (blocking: reads files)."""
    batch = Batch(folder)
    old = _old_ledger(task, folder)
    picked: dict[str, dict[str, Any]] = {}
    for f in scan_folder(folder, now):
        fp = _fp(f)
        if not fp:
            continue
        f["fp"] = fp
        row = state.db.get_materials([fp]).get(fp)
        if row is None and old.get(f["rel"]) == f"{f['size']}:{int(f['mtime'])}":
            # used by a version that kept a ledger per task
            state.db.put_material(fp, name=Path(f["path"]).name, path=f["path"], folder=str(folder), stat=f["stat"],
                                  status="used", task_id=task["id"])
            row = state.db.get_materials([fp])[fp]
        if row and row["status"] == "claimed":
            row = _settle_claim(row, running)
        status = row["status"] if row else "new"
        if status == "used":            # made into something before; this is another copy of it
            moved = move_used(folder, Path(f["path"]))
            state.db.put_material(fp, path=moved or f["path"], stat=None if moved else f["stat"], moved_to=moved or row.get("moved_to"))
            continue
        if status in ("claimed", "failed"):    # another run has it right now / gave up on it
            if status == "failed" and row:
                batch.gave_up.append(row)
            continue
        if fp in picked:
            batch.dupes.append(f)
            continue
        if len(picked) >= WATCH_MAX:
            batch.waiting += 1
            continue
        f["attempts"] = int(row["attempts"]) if row else 0
        picked[fp] = f
    batch.files = list(picked.values())
    return batch


def _settle_claim(row: dict[str, Any], running: Callable[[str], bool]) -> dict[str, Any]:
    """A claimed file whose run is no longer going (the app was closed or crashed during it)."""
    sid = row.get("session_id") or ""
    if sid and running(sid):
        return row
    made = state.db.list_posts(None, 1, session_id=sid) if sid else []
    if made:        # it got as far as making a post: that counts
        state.db.put_material(row["fp"], status="used")
    else:
        n = int(row["attempts"]) + 1
        state.db.put_material(row["fp"], status="failed" if n >= MAX_ATTEMPTS else "retry", attempts=n)
    return state.db.get_materials([row["fp"]])[row["fp"]]


def claim(batch: Batch, task_id: str, session_id: str) -> None:
    for f in batch.files:
        state.db.put_material(f["fp"], name=Path(f["path"]).name, path=f["path"], folder=str(batch.folder), stat=f["stat"],
                              status="claimed", attempts=f["attempts"], task_id=task_id, session_id=session_id)


def finish(batch: Batch, session_id: str, ok: bool) -> list[dict[str, Any]]:
    """The run is over: use the files up, or put them back for another try. Returns the ones given up on."""
    made = bool(state.db.list_posts(None, 1, session_id=session_id))
    gave_up: list[dict[str, Any]] = []
    for f in batch.files:
        if ok or made:
            moved = move_used(batch.folder, Path(f["path"]))
            state.db.put_material(f["fp"], status="used", moved_to=moved, path=moved or f["path"],
                                  stat=None if moved else f["stat"])
        else:
            n = f["attempts"] + 1
            state.db.put_material(f["fp"], status="failed" if n >= MAX_ATTEMPTS else "retry", attempts=n)
            if n >= MAX_ATTEMPTS:
                gave_up.append(f)
    if ok or made:
        for f in batch.dupes:
            move_used(batch.folder, Path(f["path"]))
    return gave_up


def retry_failed(folder: Path) -> int:
    """The user asked for files that were given up on to be tried again."""
    rows = state.db.list_materials(str(folder), "failed")
    for r in rows:
        state.db.put_material(r["fp"], status="retry", attempts=0)
    return len(rows)


def material_summary(folder: Path) -> dict[str, Any]:
    counts = state.db.material_counts(str(folder))
    failed = state.db.list_materials(str(folder), "failed", limit=20)
    waiting = 0
    if folder.is_dir():
        known = {r["path"]: r for r in state.db.list_materials(str(folder), limit=5000)}
        for f in scan_folder(folder, time.time() + WATCH_SETTLE_S):
            r = known.get(f["path"])
            if not r or r["status"] in ("retry",):
                waiting += 1
    return {"folder": str(folder), "used": counts.get("used", 0), "failed": counts.get("failed", 0),
            "running": counts.get("claimed", 0), "waiting": waiting, "used_dir": str(folder / USED_DIR),
            "failed_files": [{"name": r["name"], "path": r["path"], "attempts": r["attempts"]} for r in failed]}


def material_note(folder: Path, files: list[dict[str, Any]], waiting: int) -> str:
    def size(n: int) -> str:
        return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, n // 1024)} KB"

    head = f"这次要用的新素材（{len(files)} 个" + (f"，另有 {waiting} 个更新的留到下次" if waiting else "") + "）："
    lines = [f"【素材文件夹】{folder}", head]
    for f in files:
        retry = f"，第 {f['attempts'] + 1} 次尝试：上次没做成" if f.get("attempts") else ""
        lines.append(f"- {f['path']}（{size(f['size'])}，{dt.datetime.fromtimestamp(f['mtime']).strftime('%m-%d %H:%M')}{retry}）")
    lines.append(f"这次的作品用这些新素材来做。以前用过的素材在「{folder / USED_DIR}」里，已经发过了，不要再拿它们做一条新的；"
                 "只有需要对照（比如同一个工地的前后对比）时才可以从里面引用一小段。"
                 "不要往素材文件夹里写东西，做出来的文件放在工作区；用过的素材 AutoStudio 会自己挪进「已用过」。")
    return "\n".join(lines)


def _phone(ev: dict[str, Any]) -> None:
    from . import remote
    try:
        remote.remote.notices.publish(ev)
    except Exception:  # noqa: BLE001 - a notification must never break the scheduler
        log.warning("phone notice failed", exc_info=True)


async def send_webhook(url: str, title: str, text: str) -> int:
    """Post a message, auto-detecting popular webhook formats."""
    body = f"{title}\n\n{text}".strip()
    u = url.lower()
    async with httpx.AsyncClient(timeout=20) as client:
        if "open.feishu.cn" in u or "open.larksuite.com" in u:
            r = await client.post(url, json={"msg_type": "text", "content": {"text": body}})
        elif "oapi.dingtalk.com" in u:
            r = await client.post(url, json={"msgtype": "markdown", "markdown": {"title": title, "text": body}})
        elif "qyapi.weixin.qq.com" in u:
            r = await client.post(url, json={"msgtype": "markdown", "markdown": {"content": body[:4000]}})
        elif "api.telegram.org" in u:
            r = await client.post(url, json={"text": body[:4000]})
        elif "discord.com/api/webhooks" in u or "discordapp.com/api/webhooks" in u:
            r = await client.post(url, json={"content": body[:1900]})
        elif "hooks.slack.com" in u:
            r = await client.post(url, json={"text": body})
        elif "sctapi.ftqq.com" in u or "sc.ftqq.com" in u:
            r = await client.post(url, data={"title": title[:32], "desp": text})
        elif "api.day.app" in u:  # Bark (iOS)
            r = await client.post(url, json={"title": title, "body": text[:1000]})
        elif "ntfy" in u:
            r = await client.post(url, content=body.encode(), headers={"Title": title.encode("utf-8").decode("latin-1", "ignore")})
        else:
            r = await client.post(url, json={"title": title, "text": text})
    return r.status_code


class TaskScheduler:
    def __init__(self) -> None:
        self.sched = AsyncIOScheduler()
        self.active: dict[str, str] = {}     # task id -> session id of its run in progress
        self._catch_up: asyncio.TimerHandle | None = None

    @staticmethod
    def validate_cron(cron: str) -> CronTrigger:
        try:
            return CronTrigger.from_crontab(cron.strip())
        except ValueError as e:
            raise ValueError(f"cron 表达式无效「{cron}」：{e}。格式：分 时 日 月 周，例如 '0 9 * * *'") from e

    def start(self, catch_up_after: float | None = 90) -> None:
        for t in state.db.list_tasks():
            self.sync(t["id"])
        self.sched.start()
        if catch_up_after is not None:
            loop = asyncio.get_running_loop()
            self._catch_up = loop.call_later(catch_up_after, lambda: loop.create_task(self.catch_up()))

    def shutdown(self) -> None:
        if self._catch_up:
            self._catch_up.cancel()
        if self.sched.running:
            self.sched.shutdown(wait=False)

    def sync(self, task_id: str) -> None:
        job = self.sched.get_job(task_id)
        if job:
            job.remove()
        t = state.db.get_task(task_id)
        if not t or not t["enabled"]:
            return
        try:
            trigger = self.validate_cron(t["cron"])
        except ValueError as e:
            log.warning("task %s: %s", task_id, e)
            return
        # misfire: the PC slept through the start time and woke up later that day — run it then
        self.sched.add_job(self.run_task, trigger, args=[task_id], id=task_id, replace_existing=True,
                           misfire_grace_time=12 * 3600, coalesce=True, max_instances=1)

    @staticmethod
    def last_due(cron: str, now: dt.datetime | None = None) -> dt.datetime | None:
        """The most recent time this schedule was due (within the catch-up window), or None."""
        now = now or dt.datetime.now().astimezone()
        trig = CronTrigger.from_crontab(cron.strip(), timezone=now.tzinfo)
        prev, cursor, last = None, now - dt.timedelta(seconds=CATCH_UP_S), None
        for _ in range(2000):
            nxt = trig.get_next_fire_time(prev, cursor)
            if not nxt or nxt > now:
                break
            last, prev, cursor = nxt, nxt, nxt + dt.timedelta(seconds=1)
        return last

    async def catch_up(self) -> list[str]:
        """The app was not running when a task was due (PC off, or started later): run it now instead."""
        started: list[str] = []
        for t in state.db.list_tasks():
            if not t["enabled"]:
                continue
            try:
                due = self.last_due(t["cron"])
            except ValueError:
                continue
            if not due or (t.get("last_run_at") or 0) >= due.timestamp() or (t.get("created_at") or 0) > due.timestamp():
                continue
            log.info("task %s was due at %s while AutoStudio was not running: running it now", t["id"], due)
            r = await self.run_task(t["id"])
            if r and r.get("session_id"):
                started.append(t["id"])
        return started

    def next_run(self, task_id: str) -> str | None:
        job = self.sched.get_job(task_id)
        if job and job.next_run_time:
            return job.next_run_time.strftime("%Y-%m-%d %H:%M")
        return None

    def seconds_to_next(self) -> int | None:
        """When the next scheduled task starts, so a phone can start listening right then."""
        times = [j.next_run_time for j in self.sched.get_jobs() if getattr(j, "next_run_time", None)]
        if not times:
            return None
        return max(0, int((min(times) - dt.datetime.now(min(times).tzinfo)).total_seconds()))

    def _skip(self, t: dict[str, Any], status: str, why: str) -> dict[str, Any]:
        run_id = state.db.start_task_run(t["id"], None)
        state.db.finish_task_run(run_id, t["id"], status, why)
        return {"skipped": True, "status": status, "message": why}

    def _running(self, session_id: str) -> bool:
        return session_id in self.active.values() or bool(state.runs and state.runs.is_running(session_id))

    async def run_task(self, task_id: str) -> dict[str, Any] | None:
        """Start one run: {"session_id"}, or {"skipped", "message"} when there was nothing to do."""
        from .agent.loop import run_agent

        t = state.db.get_task(task_id)
        if not t or not state.runs:
            return None
        if task_id in self.active:
            return {"skipped": True, "message": "上一次运行还没结束"}
        prompt = t["prompt"]
        folder = watch_dir_path(t.get("watch_dir"), state.settings.workspace)
        batch: Batch | None = None
        if folder:
            if not folder.is_dir():
                why = f"素材文件夹不存在：{folder}"
                _phone({"type": "run_error", "session_id": "", "title": f"⏰ {t['name']}", "text": why, "task": True,
                        "route": "#/tasks"})
                return self._skip(t, "error", why)
            self.active[task_id] = ""       # nobody starts this task again while its folder is being read
            try:
                batch = await asyncio.to_thread(collect, t, folder, self._running)
            finally:
                self.active.pop(task_id, None)
            if not batch.files:     # nothing new: no run, no tokens, no notification
                why = f"素材文件夹里没有新文件，没有运行（{folder}）"
                if batch.gave_up:
                    why += f"；有 {len(batch.gave_up)} 个素材试了 {MAX_ATTEMPTS} 次都没做成，已跳过"
                return self._skip(t, "skipped", why)
            prompt = f"{prompt}\n\n{material_note(folder, batch.files, batch.waiting)}"

        stamp = dt.datetime.now().strftime("%m-%d %H:%M")
        session = state.db.create_session(title=f"⏰ {t['name']} · {stamp}", kind="task", task_id=task_id,
                                          provider=t.get("provider") or None, model=t.get("model") or None)
        if batch:
            claim(batch, task_id, session["id"])
        run_id = state.db.start_task_run(task_id, session["id"])
        result: dict[str, Any] = {"text": "", "status": "error"}
        self.active[task_id] = session["id"]

        async def go(run: Any) -> str:
            started = time.time()

            try:
                result["text"] = await run_agent(run, session["id"], prompt, headless=True,
                                                 allow_dangerous=bool(t.get("allow_dangerous")),
                                                 auto_publish=bool(t.get("auto_publish")))
                result["status"] = "ok"
            except Exception as e:  # noqa: BLE001
                result["text"] = f"执行失败：{e}"
                raise
            finally:

                self.active.pop(task_id, None)
                gave_up = finish(batch, session["id"], result["status"] == "ok") if batch else []
                summary = (result["text"] or "").strip()[:2000]
                pending = state.db.list_posts("pending", 50, session_id=session["id"])
                if pending:
                    summary += (f"\n\n📮 有 {len(pending)} 条内容等你确认发布："
                                + "、".join(f"「{p['content'].get('title') or '无标题'}」" for p in pending[:5])
                                + "。点手机上的通知，或打开 AutoStudio 的「发布」页确认。")
                if gave_up:
                    names = "、".join(Path(f["path"]).name for f in gave_up[:5])
                    summary += (f"\n\n⚠️ 这些素材试了 {MAX_ATTEMPTS} 次都没做成，先跳过了：{names}。"
                                "在「定时任务」页可以让它重新处理。")
                    _phone({"type": "run_error", "session_id": "", "title": f"⏰ {t['name']}", "task": True,
                            "route": "#/tasks", "text": f"{names} 试了 {MAX_ATTEMPTS} 次都没做成，先跳过了。点开可以重新处理"})
                state.db.finish_task_run(run_id, task_id, result["status"], summary)
                if t.get("webhook"):
                    took = int(time.time() - started)
                    title = f"{'✅' if result['status'] == 'ok' else '❌'} {t['name']}（{took}s）"
                    try:
                        await send_webhook(t["webhook"], title, summary or "（无输出）")
                    except Exception as e:  # noqa: BLE001
                        log.warning("webhook failed: %s", e)
            return summary      # what the phone notification shows

        state.runs.start(session["id"], go)
        return {"session_id": session["id"]}
