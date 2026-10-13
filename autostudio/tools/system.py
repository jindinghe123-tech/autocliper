"""General tools: files, shell, web, memory, downloads, scheduled tasks."""
from __future__ import annotations

import asyncio
import fnmatch
import html
import re
import shutil
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from .. import state
from ..config import MEMORY_FILE
from ..media import ffmpeg as ff
from .registry import ToolContext, ToolError, boolean, integer, obj, s, tool

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".tmp", ".frames", ".venv"}


@tool("list_files", "列出文件夹内容（默认工作区根目录）。可用 pattern 过滤（如 *.mp4），recursive 递归查找。",
      obj([], path=s("文件夹路径，默认工作区"), pattern=s("通配符，如 *.mp4"), recursive=boolean("是否递归")),
      category="file")
async def list_files(ctx: ToolContext, path: str = "", pattern: str | None = None, recursive: bool = False) -> dict[str, Any]:
    base = ctx.resolve(path) if path else ctx.workspace
    if not base.is_dir():
        raise ToolError(f"不是文件夹：{path}")
    items = []
    it = base.rglob("*") if recursive else base.iterdir()
    for p in it:
        if any(part in SKIP_DIRS for part in p.relative_to(base).parts):
            continue
        if pattern and not fnmatch.fnmatch(p.name.lower(), pattern.lower()):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        items.append({"name": ctx.rel(p) if recursive else p.name + ("/" if p.is_dir() else ""),
                      "size_mb": round(st.st_size / 1048576, 2) if p.is_file() else None,
                      "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
        if len(items) >= 500:
            break
    items.sort(key=lambda x: (not x["name"].endswith("/"), x["name"].lower()))
    return {"path": ctx.rel(base) or ".", "count": len(items), "items": items}


@tool("read_file", "读取文本文件内容（字幕、脚本、markdown、json、csv 等）。",
      obj(["path"], path=s("文件路径"), max_chars=integer("最多读取字符数，默认 20000")), category="file")
async def read_file(ctx: ToolContext, path: str, max_chars: int = 20000) -> str:
    p = ctx.resolve(path, must_exist=True)
    if p.suffix.lower() in ff.VIDEO_EXTS | ff.AUDIO_EXTS | ff.IMAGE_EXTS:
        raise ToolError("这是媒体文件，请用 media_info / view_frames 等工具")
    text = p.read_text("utf-8", errors="replace")
    return text if len(text) <= max_chars else text[:max_chars] + f"\n…[截断，共 {len(text)} 字符]"


@tool("write_file", "写入文本文件（脚本、文案、字幕、markdown 报告等）。append=true 时追加。",
      obj(["path", "content"], path=s("文件路径"), content=s("内容"), append=boolean("追加模式")), category="file")
async def write_file(ctx: ToolContext, path: str, content: str, append: bool = False) -> dict[str, Any]:
    p = ctx.resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if append else "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "path": ctx.rel(p), "chars": len(content), "files": [str(p)]}


@tool("file_manage", "文件管理：copy 复制 / move 移动或重命名 / mkdir 新建文件夹 / trash 移到工作区回收站（.trash）。",
      obj(["action", "path"], action=s("操作", enum=["copy", "move", "mkdir", "trash"]), path=s("源路径"),
          target=s("目标路径（copy/move 需要）")), category="file")
async def file_manage(ctx: ToolContext, action: str, path: str, target: str | None = None) -> dict[str, Any]:
    p = ctx.resolve(path)
    if action == "mkdir":
        p.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": ctx.rel(p)}
    if not p.exists():
        raise ToolError(f"不存在：{path}")
    if action == "trash":
        trash = ctx.workspace / ".trash"
        trash.mkdir(exist_ok=True)
        dest = trash / f"{int(time.time())}_{p.name}"
        shutil.move(str(p), dest)
        return {"ok": True, "moved_to": ctx.rel(dest)}
    if not target:
        raise ToolError("需要 target")
    t = ctx.resolve(target)
    if t.is_dir():
        t = t / p.name
    t.parent.mkdir(parents=True, exist_ok=True)
    if action == "copy":
        (shutil.copytree if p.is_dir() else shutil.copy2)(p, t)
    else:
        shutil.move(str(p), t)
    return {"ok": True, "path": ctx.rel(t), "files": [str(t)] if t.is_file() else []}


@tool("run_command",
      "在本机执行命令行（Windows 为 PowerShell，macOS/Linux 为 bash）。可用于安装软件、批量处理、调用其它工具。需要用户批准。",
      obj(["command"], command=s("命令"), cwd=s("工作目录，默认工作区"), timeout=integer("超时秒数，默认 300")),
      category="system", dangerous=True)
async def run_command(ctx: ToolContext, command: str, cwd: str | None = None, timeout: int = 300) -> dict[str, Any]:
    wd = ctx.resolve(cwd) if cwd else ctx.workspace
    if ff.IS_WIN:
        args = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
    else:
        args = ["bash", "-lc", command]
    try:
        code, out, err = await ff.run_proc(args, cwd=wd, timeout=timeout)
    except asyncio.TimeoutError as e:
        raise ToolError(f"命令超时（{timeout}s）") from e
    trim = lambda t: t if len(t) < 12000 else t[:4000] + "\n…\n" + t[-7000:]  # noqa: E731
    return {"exit_code": code, "stdout": trim(out), "stderr": trim(err)}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr", "section", "article"):
            self.parts.append("\n")
        if tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header") and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a":
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self.skip:
            return
        self.parts.append(data)
        if self._href and data.strip() and len(self.links) < 60:
            self.links.append((data.strip()[:80], self._href))


@tool("web_fetch", "抓取网页并提取正文文字（也可读取 RSS/JSON/纯文本）。用于获取资讯、读文章、查资料。",
      obj(["url"], url=s("网址"), max_chars=integer("最多返回字符数，默认 15000"),
          include_links=boolean("是否附带页面链接列表")), category="web")
async def web_fetch(ctx: ToolContext, url: str, max_chars: int = 15000, include_links: bool = False) -> dict[str, Any]:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/126.0 Safari/537.36", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        r = await client.get(url)
        if r.status_code == 403 and ("robot policy" in r.text or "wikipedia.org" in url):
            r = await client.get(url, headers={"User-Agent": "AutoStudio/0.1 (+https://github.com/jindinghe123-tech/autocliper)"})
    ctype = r.headers.get("content-type", "")
    text = r.text
    result: dict[str, Any] = {"url": str(r.url), "status": r.status_code}
    if "html" in ctype or text.lstrip().lower().startswith(("<!doctype html", "<html")):
        ex = _TextExtractor()
        ex.feed(text)
        body = html.unescape("".join(ex.parts))
        body = re.sub(r"[ \t\r\f\v]+", " ", body)
        body = re.sub(r"\n\s*\n+", "\n\n", body).strip()
        result["title"] = ex.title.strip()
        result["text"] = body[:max_chars]
        if include_links:
            result["links"] = [{"text": t, "href": str(httpx.URL(str(r.url)).join(h))} for t, h in ex.links]
    else:
        result["text"] = text[:max_chars]
    return result


@tool("memory_update", "更新长期记忆（会在每次对话时提供给你）：记录用户偏好，如常用字幕样式、频道风格、固定片头、常用音乐目录等。"
      "mode=append 追加一条，replace 整体替换。",
      obj(["content"], content=s("记忆内容"), mode=s("append / replace", enum=["append", "replace"])), category="system")
async def memory_update(ctx: ToolContext, content: str, mode: str = "append") -> dict[str, Any]:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if mode == "replace":
        MEMORY_FILE.write_text(content.strip() + "\n", "utf-8")
    else:
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"- {content.strip()}\n")
    return {"ok": True, "memory": MEMORY_FILE.read_text("utf-8")[-3000:]}


@tool("download_media", "下载在线视频/音频（支持 B 站、YouTube、抖音等上千个网站，基于 yt-dlp）。请仅下载你有权使用的内容。",
      obj(["url"], url=s("视频链接"), audio_only=boolean("只下载音频(mp3)"),
          max_height=integer("最高分辨率，默认 1080")), category="web")
async def download_media(ctx: ToolContext, url: str, audio_only: bool = False, max_height: int = 1080) -> dict[str, Any]:
    try:
        import yt_dlp  # type: ignore
    except ImportError as e:
        raise ToolError("需要安装 yt-dlp：pip install yt-dlp") from e
    out_dir = ctx.workspace / "downloads"
    out_dir.mkdir(exist_ok=True)
    loop = asyncio.get_running_loop()
    files: list[str] = []

    def hook(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                frac = d.get("downloaded_bytes", 0) / total
                loop.call_soon_threadsafe(lambda: loop.create_task(ctx.progress(frac * 0.95, "下载中")))
        elif d.get("status") == "finished" and d.get("filename"):
            files.append(d["filename"])

    opts: dict[str, Any] = {
        "outtmpl": str(out_dir / "%(title).60s [%(id)s].%(ext)s"),
        "progress_hooks": [hook], "quiet": True, "no_warnings": True, "noplaylist": True,
        "ffmpeg_location": ff.ffmpeg_bin(), "windowsfilenames": True,
    }
    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
    else:
        opts["format"] = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
        opts["merge_output_format"] = "mp4"

    def run() -> dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            return {"title": info.get("title"), "duration": info.get("duration"), "path": path,
                    "uploader": info.get("uploader")}

    info = await asyncio.to_thread(run)
    p = Path(info["path"])
    if audio_only:
        p = p.with_suffix(".mp3")
    elif not p.exists():
        p = p.with_suffix(".mp4")
    if not p.exists() and files:
        p = Path(files[-1])
    return {"ok": True, "title": info["title"], "duration": info["duration"], "output": ctx.rel(p), "files": [str(p)]}


# ---- scheduled tasks ------------------------------------------------------------

def _task_view(t: dict[str, Any]) -> dict[str, Any]:
    nxt = state.scheduler.next_run(t["id"]) if state.scheduler else None
    return {"id": t["id"], "name": t["name"], "cron": t["cron"], "enabled": bool(t["enabled"]),
            "prompt": t["prompt"][:300], "webhook": bool(t.get("webhook")), "next_run": nxt,
            "auto_publish": bool(t.get("auto_publish")), "last_status": t.get("last_status")}


@tool("task_create",
      "创建定时自动任务（每日/每周/每小时执行）。到点后会自动开启一个新对话，用 prompt 作为指令执行，结果可推送到 webhook"
      "（飞书/钉钉/企业微信/Telegram/Discord/Slack/Server酱/通用）。cron 为 5 段式：分 时 日 月 周，如 '0 9 * * *' 每天 9 点，"
      "'30 20 * * 1-5' 工作日 20:30，'0 */2 * * *' 每两小时。",
      obj(["name", "prompt", "cron"], name=s("任务名"), prompt=s("每次执行时给 agent 的完整指令（要写清楚输入在哪、输出到哪）"),
          cron=s("cron 表达式"), webhook=s("结果推送地址（可选）"),
          allow_dangerous=boolean("是否允许执行命令行等危险操作（无人值守，默认 false）"),
          auto_publish=boolean("发布到社交平台时直接发出、不等用户确认（默认 false：进入待确认队列）")), category="task")
async def task_create(ctx: ToolContext, name: str, prompt: str, cron: str, webhook: str | None = None,
                      allow_dangerous: bool = False, auto_publish: bool = False) -> dict[str, Any]:
    if not state.scheduler:
        raise ToolError("调度器未运行")
    state.scheduler.validate_cron(cron)
    t = state.db.create_task(name=name, prompt=prompt, cron=cron, enabled=True, webhook=webhook,
                             allow_dangerous=allow_dangerous, auto_publish=auto_publish)
    state.scheduler.sync(t["id"])
    return {"ok": True, "task": _task_view(state.db.get_task(t["id"]) or t)}


@tool("task_list", "列出所有定时任务及下次运行时间。", obj([]), category="task")
async def task_list(ctx: ToolContext) -> dict[str, Any]:
    return {"tasks": [_task_view(t) for t in state.db.list_tasks()]}


@tool("task_update", "修改定时任务（名称、指令、cron、启用/停用、webhook、是否自动发布）。",
      obj(["id"], id=s("任务 id"), name=s("新名称"), prompt=s("新指令"), cron=s("新 cron"),
          enabled=boolean("启用/停用"), webhook=s("推送地址"),
          auto_publish=boolean("发布到社交平台时是否直接发出（不等用户确认）")), category="task")
async def task_update(ctx: ToolContext, id: str, **fields: Any) -> dict[str, Any]:
    if not state.db.get_task(id):
        raise ToolError(f"任务不存在：{id}")
    if fields.get("cron") and state.scheduler:
        state.scheduler.validate_cron(fields["cron"])
    t = state.db.update_task(id, **{k: v for k, v in fields.items() if v is not None})
    if state.scheduler:
        state.scheduler.sync(id)
    return {"ok": True, "task": _task_view(t or {})}


@tool("task_delete", "删除定时任务。", obj(["id"], id=s("任务 id")), category="task")
async def task_delete(ctx: ToolContext, id: str) -> dict[str, Any]:
    if not state.db.get_task(id):
        raise ToolError(f"任务不存在：{id}")
    state.db.delete_task(id)
    if state.scheduler:
        state.scheduler.sync(id)
    return {"ok": True}


@tool("notify", "主动推送一条消息到 webhook（飞书/钉钉/企业微信/Telegram/Discord/Slack/Server酱/通用 JSON）。定时任务汇报结果时可用。",
      obj(["webhook", "text"], webhook=s("推送地址"), text=s("消息内容"), title=s("标题")), category="task")
async def notify(ctx: ToolContext, webhook: str, text: str, title: str = "AutoStudio") -> dict[str, Any]:
    from ..scheduler import send_webhook

    status = await send_webhook(webhook, title, text)
    return {"ok": status < 400, "status": status}
