"""Cron-based scheduled agent tasks + result push to chat webhooks."""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import state

log = logging.getLogger("autostudio.scheduler")


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

    async def run_task(self, task_id: str) -> str | None:
        from .agent.loop import run_agent

        t = state.db.get_task(task_id)
        if not t or not state.runs:
            return None
        stamp = dt.datetime.now().strftime("%m-%d %H:%M")
        session = state.db.create_session(title=f"⏰ {t['name']} · {stamp}", kind="task", task_id=task_id,
                                          provider=t.get("provider") or None, model=t.get("model") or None)
        run_id = state.db.start_task_run(task_id, session["id"])
        result: dict[str, Any] = {"text": "", "status": "error"}

        async def go(run: Any) -> str:
            started = time.time()
            try:
                result["text"] = await run_agent(run, session["id"], t["prompt"], headless=True,
                                                 allow_dangerous=bool(t.get("allow_dangerous")),
                                                 auto_publish=bool(t.get("auto_publish")))
                result["status"] = "ok"
            except Exception as e:  # noqa: BLE001
                result["text"] = f"执行失败：{e}"
                raise
            finally:
                summary = (result["text"] or "").strip()[:2000]
                pending = state.db.list_posts("pending", 50, session_id=session["id"])
                if pending:
                    summary += (f"\n\n📮 有 {len(pending)} 条内容等你确认发布："
                                + "、".join(f"「{p['content'].get('title') or '无标题'}」" for p in pending[:5])
                                + "。打开 AutoStudio →「发布」页点一下就发。")
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
        return session["id"]
