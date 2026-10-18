"""Cron-based scheduled agent tasks + result push to chat webhooks."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import state

log = logging.getLogger("autostudio.scheduler")

# ---------------------------------------------------------------------------
# material folder: each run is handed only what was put there since the last successful run

WATCH_MAX = 30          # files handed to one run; the rest wait for the next one
WATCH_SETTLE_S = 60     # changed this recently: maybe still being copied in, so it waits for the next run
WATCH_SKIP = re.compile(r"^[.~]|\.(tmp|temp|part|partial|crdownload|download)$|^(thumbs\.db|desktop\.ini)$", re.I)


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


def _ledger(task: dict[str, Any], folder: Path) -> dict[str, str]:
    try:
        seen = json.loads(task.get("watch_seen") or "{}")
    except ValueError:
        return {}
    return (seen.get("files") or {}) if seen.get("dir") == str(folder) else {}


def scan_new(folder: Path, seen: dict[str, str], now: float | None = None) -> list[dict[str, Any]]:
    """Files under `folder` that are not in `seen` (or changed since), oldest first."""
    now = time.time() if now is None else now
    out: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not WATCH_SKIP.search(d))
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
            rel = path.relative_to(folder).as_posix()
            key = f"{st.st_size}:{int(st.st_mtime)}"
            if seen.get(rel) != key:
                out.append({"path": str(path), "rel": rel, "key": key, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda f: (f["mtime"], f["rel"]))
    return out


def material_note(folder: Path, files: list[dict[str, Any]], waiting: int) -> str:
    def size(n: int) -> str:
        return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, n // 1024)} KB"

    head = f"上次运行之后放进来的新文件（{len(files)} 个" + (f"，另有 {waiting} 个更新的留到下次" if waiting else "") + "）："
    lines = [f"【素材文件夹】{folder}", head]
    lines += [f"- {f['path']}（{size(f['size'])}，{dt.datetime.fromtimestamp(f['mtime']).strftime('%m-%d %H:%M')}）"
              for f in files]
    lines.append("这次只用这些新文件。不要往素材文件夹里写东西，做出来的文件放在工作区。")
    return "\n".join(lines)


def mark_seen(task_id: str, folder: Path, files: list[dict[str, Any]]) -> None:
    """Remember the files a successful run was given; forget ones that have left the folder."""
    t = state.db.get_task(task_id)
    if not t:
        return
    seen = {rel: key for rel, key in _ledger(t, folder).items() if (folder / rel).exists()}
    seen.update({f["rel"]: f["key"] for f in files})
    state.db.set_task_seen(task_id, {"dir": str(folder), "files": seen})


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

    @staticmethod
    def validate_cron(cron: str) -> CronTrigger:
        try:
            return CronTrigger.from_crontab(cron.strip())
        except ValueError as e:
            raise ValueError(f"cron 表达式无效「{cron}」：{e}。格式：分 时 日 月 周，例如 '0 9 * * *'") from e

    def start(self) -> None:
        for t in state.db.list_tasks():
            self.sync(t["id"])
        self.sched.start()

    def shutdown(self) -> None:
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
        self.sched.add_job(self.run_task, trigger, args=[task_id], id=task_id, replace_existing=True,
                           misfire_grace_time=3600, coalesce=True, max_instances=1)

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
        files: list[dict[str, Any]] = []
        if folder:
            if not folder.is_dir():
                why = f"素材文件夹不存在：{folder}"
                _phone({"type": "run_error", "session_id": "", "title": f"⏰ {t['name']}", "text": why, "task": True,
                        "route": "#/tasks"})
                return self._skip(t, "error", why)
            new = await asyncio.to_thread(scan_new, folder, _ledger(t, folder))
            if not new:     # nothing new: no run, no tokens, no notification
                return self._skip(t, "skipped", f"素材文件夹里没有新文件，没有运行（{folder}）")
            files = new[:WATCH_MAX]
            prompt = f"{prompt}\n\n{material_note(folder, files, len(new) - len(files))}"

        stamp = dt.datetime.now().strftime("%m-%d %H:%M")
        session = state.db.create_session(title=f"⏰ {t['name']} · {stamp}", kind="task", task_id=task_id,
                                          provider=t.get("provider") or None, model=t.get("model") or None)
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
                if folder and files:     # a failed run gets the same files again next time
                    mark_seen(task_id, folder, files)
            except Exception as e:  # noqa: BLE001
                result["text"] = f"执行失败：{e}"
                raise
            finally:
                self.active.pop(task_id, None)
                summary = (result["text"] or "").strip()[:2000]
                pending = state.db.list_posts("pending", 50, session_id=session["id"])
                if pending:
                    summary += (f"\n\n📮 有 {len(pending)} 条内容等你确认发布："
                                + "、".join(f"「{p['content'].get('title') or '无标题'}」" for p in pending[:5])
                                + "。点手机上的通知，或打开 AutoStudio 的「发布」页确认。")
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
