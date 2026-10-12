"""Common provider interface.

Internal message format (stored in the DB, OpenAI-chat flavoured):
  {"role": "user", "content": str, "images": [path, ...]}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments"}], "reasoning": str,
   "reasoning_content": str}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str, "images": [path, ...]}

Each provider converts this into its own wire format.
"""
from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

Emit = Callable[[dict[str, Any]], Awaitable[None]]

TIMEOUT = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=20.0)


class ProviderError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class TurnResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str = ""
    reasoning_content: str = ""     # raw provider reasoning that some APIs need echoed back
    usage: dict[str, Any] | None = None


class Provider:
    vision: bool = False

    async def stream(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     emit: Emit) -> TurnResult:
        raise NotImplementedError

    async def list_models(self) -> list[str]:
        return []


async def iter_sse(resp: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """Yield (event, data) pairs from a text/event-stream response."""
    event, data_lines = "", []
    async for line in resp.aiter_lines():
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event, data_lines = "", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if data_lines:
        yield event, "\n".join(data_lines)


async def raise_for_status(resp: httpx.Response, label: str) -> None:
    if resp.status_code < 400:
        return
    body = (await resp.aread()).decode("utf-8", "replace")
    msg = body
    try:
        j = json.loads(body)
        err = j.get("error", j) if isinstance(j, dict) else j
        if isinstance(err, dict):
            msg = err.get("message") or err.get("detail") or json.dumps(err, ensure_ascii=False)
        elif isinstance(j, dict) and j.get("detail"):
            msg = str(j["detail"])
    except Exception:
        pass
    raise ProviderError(f"{label} 请求失败 (HTTP {resp.status_code}): {msg[:800]}")


def image_data_url(path: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def image_b64(path: str) -> tuple[str, str] | None:
    p = Path(path)
    if not p.is_file():
        return None
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    return mime, base64.b64encode(p.read_bytes()).decode()


def tool_args_obj(arguments: str) -> dict[str, Any]:
    try:
        v = json.loads(arguments or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"_raw": arguments}
