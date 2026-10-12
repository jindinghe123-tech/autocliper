"""OpenAI Responses API — used for OpenAI API keys and for the ChatGPT-subscription (Codex) backend."""
from __future__ import annotations

import json
import platform
import uuid
from typing import Any

import httpx

from ..config import DATA_DIR
from .base import (TIMEOUT, Emit, Provider, ProviderError, ToolCall, TurnResult, image_data_url, iter_sse,
                   raise_for_status)
from .chatgpt_auth import chatgpt_auth

CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
CODEX_PROMPT_URLS = [
    "https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/prompt.md",
    "https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/gpt_5_codex_prompt.md",
]
CODEX_PROMPT_CACHE = DATA_DIR / "codex_instructions.md"


class ResponsesProvider(Provider):
    # If the ChatGPT backend rejects custom `instructions`, we switch to sending the official Codex
    # instructions and move our system prompt into a developer message. Remembered per process.
    _codex_instructions_mode = False

    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1", api_key: str = "",
                 chatgpt: bool = False, reasoning_effort: str | None = None, vision: bool = True,
                 session_id: str | None = None):
        self.model = model
        self.base_url = (CHATGPT_BASE if chatgpt else base_url).rstrip("/")
        self.api_key = api_key
        self.chatgpt = chatgpt
        self.reasoning_effort = self._resolve_effort(reasoning_effort) if chatgpt else (reasoning_effort or None)
        self.vision = vision
        self.session_id = session_id or uuid.uuid4().hex

    def _resolve_effort(self, wanted: str | None) -> str | None:
        """'auto' -> the model's own default; unsupported levels fall back to the closest supported one."""
        from .catalog import model_meta

        meta = model_meta(self.model) or {}
        efforts = meta.get("efforts") or []
        if not wanted or wanted == "auto":
            return meta.get("default_effort") or "medium"
        if not efforts or wanted in efforts:
            return wanted
        order = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
        rank = order.index(wanted) if wanted in order else 2
        supported = sorted((e for e in efforts if e in order), key=order.index)
        below = [e for e in supported if order.index(e) <= rank]
        return (below or supported or [meta.get("default_effort") or "medium"])[-1 if below else 0]

    def convert(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        pending_images: list[str] = []

        def flush_images() -> None:
            if pending_images and self.vision:
                content: list[dict[str, Any]] = [{"type": "input_text", "text": "（以上工具返回的图片）"}]
                for p in pending_images:
                    url = image_data_url(p)
                    if url:
                        content.append({"type": "input_image", "image_url": url})
                items.append({"type": "message", "role": "user", "content": content})
            pending_images.clear()

        for m in messages:
            role = m["role"]
            if role != "tool":
                flush_images()
            if role == "user":
                content = [{"type": "input_text", "text": m.get("content") or ""}]
                if self.vision:
                    for p in m.get("images") or []:
                        url = image_data_url(p)
                        if url:
                            content.append({"type": "input_image", "image_url": url})
                items.append({"type": "message", "role": "user", "content": content})
            elif role == "assistant":
                if m.get("content"):
                    items.append({"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": m["content"]}]})
                for tc in m.get("tool_calls") or []:
                    items.append({"type": "function_call", "call_id": tc["id"], "name": tc["name"],
                                  "arguments": tc.get("arguments") or "{}"})
            elif role == "tool":
                items.append({"type": "function_call_output", "call_id": m["tool_call_id"],
                              "output": m.get("content") or ""})
                pending_images.extend(m.get("images") or [])
        flush_images()
        return items

    async def _headers(self, force_refresh: bool = False) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.chatgpt:
            token, account_id = await chatgpt_auth.credentials(force_refresh)
            h.update({
                "Authorization": f"Bearer {token}",
                "chatgpt-account-id": account_id,
                "OpenAI-Beta": "responses=experimental",
                "originator": "codex_cli_rs",
                "session_id": self.session_id,
                "User-Agent": f"autostudio ({platform.system()} {platform.release()})",
            })
        elif self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def _codex_instructions(self) -> str:
        from .catalog import base_instructions

        official = base_instructions(self.model)
        if official:
            return official
        if CODEX_PROMPT_CACHE.exists():
            return CODEX_PROMPT_CACHE.read_text("utf-8")
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for url in CODEX_PROMPT_URLS:
                try:
                    r = await client.get(url)
                    if r.status_code == 200 and len(r.text) > 200:
                        CODEX_PROMPT_CACHE.parent.mkdir(parents=True, exist_ok=True)
                        CODEX_PROMPT_CACHE.write_text(r.text, "utf-8")
                        return r.text
                except httpx.HTTPError:
                    continue
        raise ProviderError("ChatGPT 后端要求官方 Codex 指令，但无法从 GitHub 下载。请检查网络。")

    async def _body(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        input_items = self.convert(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "stream": True,
            "store": False,
        }
        if self.chatgpt and ResponsesProvider._codex_instructions_mode:
            body["instructions"] = await self._codex_instructions()
            body["input"] = [{"type": "message", "role": "developer",
                              "content": [{"type": "input_text", "text": system}]}] + input_items
        else:
            body["instructions"] = system
        if tools:
            body["tools"] = [{"type": "function", "name": t["name"], "description": t["description"],
                              "parameters": t["parameters"], "strict": False} for t in tools]
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort, "summary": "auto"}
        if self.chatgpt:
            body["prompt_cache_key"] = self.session_id
        return body

    async def stream(self, system, messages, tools, emit: Emit) -> TurnResult:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                return await self._stream_once(system, messages, tools, emit, force_refresh=attempt > 0)
            except _AuthExpired as e:
                last_err = e
                continue
            except _BadInstructions as e:
                if ResponsesProvider._codex_instructions_mode:
                    raise ProviderError(str(e)) from e
                ResponsesProvider._codex_instructions_mode = True
                last_err = e
                continue
        raise ProviderError(str(last_err))

    async def _stream_once(self, system, messages, tools, emit: Emit, force_refresh: bool) -> TurnResult:
        body = await self._body(system, messages, tools)
        headers = await self._headers(force_refresh)
        result = TurnResult()
        label = "ChatGPT" if self.chatgpt else self.model
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            async with client.stream("POST", f"{self.base_url}/responses", headers=headers, json=body) as resp:
                if self.chatgpt and resp.status_code == 401:
                    raise _AuthExpired("ChatGPT 登录已过期")
                if resp.status_code == 400:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    if self.chatgpt and "instruction" in text.lower():
                        raise _BadInstructions(text[:500])
                    raise ProviderError(f"{label} 请求失败 (HTTP 400): {_err_message(text)}")
                if resp.status_code == 429:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    raise ProviderError(f"{label} 用量已达上限或请求过快 (429)：{_err_message(text)}")
                await raise_for_status(resp, label)
                async for event, data in iter_sse(resp):
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    etype = ev.get("type") or event
                    if etype == "response.output_text.delta":
                        d = ev.get("delta") or ""
                        result.text += d
                        await emit({"type": "text_delta", "delta": d})
                    elif etype in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                        d = ev.get("delta") or ""
                        result.reasoning += d
                        await emit({"type": "reasoning_delta", "delta": d})
                    elif etype == "response.reasoning_summary_part.done":
                        result.reasoning += "\n\n"
                    elif etype == "response.output_item.done":
                        item = ev.get("item") or {}
                        if item.get("type") == "function_call":
                            result.tool_calls.append(ToolCall(
                                id=item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                                name=item.get("name", ""), arguments=item.get("arguments") or "{}"))
                    elif etype == "response.completed":
                        result.usage = (ev.get("response") or {}).get("usage")
                    elif etype in ("response.failed", "error", "response.error"):
                        err = (ev.get("response") or {}).get("error") or ev.get("error") or ev
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                        raise ProviderError(f"{label} 返回错误：{msg}")
                    elif etype == "response.incomplete":
                        reason = ((ev.get("response") or {}).get("incomplete_details") or {}).get("reason")
                        await emit({"type": "notice", "text": f"回复被截断：{reason}"})
        return result

    async def list_models(self) -> list[str]:
        from .catalog import fetch_chatgpt_catalog, sort_and_filter

        if self.chatgpt:
            return [m["slug"] for m in await fetch_chatgpt_catalog(force=True)]
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
            if r.status_code >= 400:
                raise ProviderError(f"获取模型列表失败 (HTTP {r.status_code}): {r.text[:300]}")
            return sort_and_filter(r.json().get("data", []))


class _AuthExpired(Exception):
    pass


class _BadInstructions(Exception):
    pass


def _err_message(text: str) -> str:
    try:
        j = json.loads(text)
        err = j.get("error") or j.get("detail") or j
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return str(err)
    except Exception:
        return text[:500]
