"""Common provider interface.

Internal message format (stored in the DB, OpenAI-chat flavoured):
  {"role": "user", "content": str, "images": [path, ...]}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments"}], "reasoning": str,
   "reasoning_content": str}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str, "images": [path, ...]}

Each provider converts this into its own wire format.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import ssl
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

Emit = Callable[[dict[str, Any]], Awaitable[None]]

TIMEOUT = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=20.0)
# Gateway hiccups worth retrying. 507 = the request was too big for the gateway to buffer for its own retry.
TRANSIENT_STATUS = {500, 502, 503, 504, 507, 520, 522, 524, 529}
IMG_MAX_SIDE = 1280
IMG_MAX_BYTES = 100_000


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


async def stream_retrying(provider: Provider, system: str, messages: list[dict[str, Any]],
                          tools: list[dict[str, Any]], emit: Emit,
                          on_retry: Callable[[int, Exception], Awaitable[None]] | None = None,
                          attempts: int = 4) -> TurnResult:
    """provider.stream, generated again when the connection drops mid-answer.

    Nothing an answer asks for has run yet when its stream breaks, so a fresh attempt is safe;
    `on_retry` lets the caller throw away whatever half answer it already showed.
    """
    for n in range(1, attempts + 1):
        try:
            return await provider.stream(system, messages, tools, emit)
        except (httpx.TransportError, ssl.SSLError, ConnectionError) as e:
            if n == attempts:
                raise ProviderError(f"与模型的连接反复中断（{e}），请稍后发送「继续」") from e
            if on_retry:
                await on_retry(n, e)
            await asyncio.sleep(2 * n)
    raise AssertionError("unreachable")


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


def compact_image(p: Path) -> tuple[Path, str]:
    """Images ride along as base64 in *every* request of a conversation, so keep each one small:
    anything over ~100 KB (phone photos, 4K frames, big contact sheets) becomes a ≤1280 px JPEG (cached)."""
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    st = p.stat()
    if st.st_size <= IMG_MAX_BYTES and mime in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        return p, mime
    from ..config import DATA_DIR
    from ..media.ffmpeg import CREATE_NO_WINDOW, ffmpeg_bin

    key = hashlib.sha1(f"{p.resolve()}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:20]
    out = DATA_DIR / "cache" / "img" / f"{key}.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(p), "-frames:v", "1", "-q:v", "5", "-vf",
                            f"scale='min({IMG_MAX_SIDE},iw)':'min({IMG_MAX_SIDE},ih)':force_original_aspect_ratio=decrease",
                            str(out)], capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
        except Exception:  # noqa: BLE001 - fall back to the original file
            pass
        if not out.exists():
            return p, mime
    return out, "image/jpeg"


def image_data_url(path: str) -> str | None:
    got = image_b64(path)
    return f"data:{got[0]};base64,{got[1]}" if got else None


def image_b64(path: str) -> tuple[str, str] | None:
    p = Path(path)
    if not p.is_file():
        return None
    p, mime = compact_image(p)
    return mime, base64.b64encode(p.read_bytes()).decode()


def lean_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the most recent message's images (used when a request turned out to be too large)."""
    out = [dict(m) for m in messages]
    last = max((i for i, m in enumerate(out) if m.get("images")), default=-1)
    for i, m in enumerate(out):
        if m.get("images") and i != last:
            m["images"] = []
    return out


def tool_args_obj(arguments: str) -> dict[str, Any]:
    try:
        v = json.loads(arguments or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"_raw": arguments}
