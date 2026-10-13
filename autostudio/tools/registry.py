"""Tool registry, schema helpers and the per-call ToolContext."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

Emit = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    category: str = "general"
    dangerous: bool = False

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


REGISTRY: dict[str, Tool] = {}

CATEGORY_LABELS = {
    "video": "视频剪辑",
    "creative": "创意效果",
    "audio": "音频",
    "subtitle": "字幕",
    "file": "文件",
    "web": "网络",
    "system": "系统",
    "task": "定时任务",
    "social": "社交发布",
    "assets": "素材搜索",
}


def tool(name: str, description: str, parameters: dict[str, Any], *, category: str = "general",
         dangerous: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY[name] = Tool(name, description.strip(), parameters, fn, category, dangerous)
        return fn

    return deco


# ---- JSON-schema shorthands -------------------------------------------------

def obj(required: list[str] | None = None, **props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


def s(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **kw}


def num(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "number", "description": desc, **kw}


def integer(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "integer", "description": desc, **kw}


def boolean(desc: str) -> dict[str, Any]:
    return {"type": "boolean", "description": desc}


def arr(items: dict[str, Any], desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "array", "items": items, "description": desc, **kw}


def time_s(desc: str) -> dict[str, Any]:
    return {"type": ["number", "string"], "description": desc + "（秒数或 HH:MM:SS.mmm）"}


def any_of_obj(desc: str) -> dict[str, Any]:
    return {"type": "object", "description": desc, "additionalProperties": True}


# ---- runtime context -----------------------------------------------------------

class ToolError(Exception):
    """Expected failure with a message meant for the model."""


@dataclass
class ToolContext:
    session_id: str
    workspace: Path
    emit: Emit
    call_id: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    headless: bool = False
    publish_mode: str = "now"   # social_publish: "now" or "queue" (wait for the user's approval)
    _last_progress: float = 0.0
    _tmpdirs: list[str] = field(default_factory=list)

    def resolve(self, p: str | os.PathLike[str] | None, must_exist: bool = False) -> Path:
        if p is None or str(p).strip() == "":
            raise ToolError("缺少文件路径")
        raw = str(p).strip().strip('"').strip("'")
        if raw.startswith("file://"):
            raw = raw[7:]
        path = Path(os.path.expandvars(raw)).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve()
        if must_exist and not path.exists():
            raise ToolError(f"文件不存在：{p}（工作区：{self.workspace}）")
        return path

    def rel(self, p: Path | str) -> str:
        """Display path: relative to workspace when inside it."""
        p = Path(p)
        try:
            return p.resolve().relative_to(self.workspace.resolve()).as_posix()
        except ValueError:
            return str(p)

    def out_path(self, src: Path | str | None, suffix: str, ext: str | None = None, output: str | None = None,
                 subdir: str = "outputs") -> Path:
        if output:
            p = self.resolve(output)
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        src_p = Path(src) if src else Path("output")
        ext = ext or src_p.suffix or ".mp4"
        d = self.workspace / subdir
        d.mkdir(parents=True, exist_ok=True)
        stem = src_p.stem
        candidate = d / f"{stem}{suffix}{ext}"
        n = 2
        while candidate.exists():
            candidate = d / f"{stem}{suffix}_{n}{ext}"
            n += 1
        return candidate

    def tmpdir(self) -> Path:
        base = self.workspace / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        d = tempfile.mkdtemp(prefix="op_", dir=base)
        self._tmpdirs.append(d)
        return Path(d)

    def cleanup(self) -> None:
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
        self._tmpdirs.clear()

    async def progress(self, fraction: float, text: str = "") -> None:
        now = time.monotonic()
        if fraction < 0.999 and now - self._last_progress < 0.4 and not text:
            return
        self._last_progress = now
        await self.emit({"type": "tool_progress", "id": self.call_id, "progress": round(max(0.0, min(1.0, fraction)), 3),
                         "text": text})

    def progress_cb(self, lo: float = 0.0, hi: float = 1.0, text: str = "") -> Callable[[float], None]:
        """Sync callback (for ffmpeg) mapping 0..1 into lo..hi."""
        loop = asyncio.get_running_loop()

        def cb(f: float) -> None:
            loop.create_task(self.progress(lo + (hi - lo) * f, text))

        return cb


async def call_tool(t: Tool, ctx: ToolContext, args: dict[str, Any]) -> Any:
    sig = inspect.signature(t.func)
    accepted = {k: v for k, v in args.items() if k in sig.parameters}
    unknown = [k for k in args if k not in sig.parameters]
    if unknown and not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
        accepted_names = [p for p in sig.parameters if p != "ctx"]
        if not accepted:
            raise ToolError(f"参数不匹配：{unknown}。可用参数：{accepted_names}")
    if inspect.iscoroutinefunction(t.func):
        return await t.func(ctx, **accepted)
    return await asyncio.to_thread(t.func, ctx, **accepted)


def to_text(result: Any, limit: int = 24000) -> str:
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False, indent=1, default=str)
    if len(text) > limit:
        head, tail = text[: int(limit * 0.7)], text[-int(limit * 0.25):]
        text = f"{head}\n…[输出过长，已省略 {len(text) - len(head) - len(tail)} 字符]…\n{tail}"
    return text
