"""Anthropic Messages API (Claude) — also works with Anthropic-compatible endpoints."""
from __future__ import annotations

import json
from typing import Any

import httpx

from .base import TIMEOUT, Emit, Provider, ProviderError, ToolCall, TurnResult, image_b64, iter_sse, raise_for_status


class AnthropicProvider(Provider):
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.anthropic.com",
                 vision: bool = True, max_tokens: int = 16000):
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.vision = vision
        self.max_tokens = max_tokens

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}

    def _url(self, path: str) -> str:
        base = self.base_url
        return f"{base}{path}" if base.endswith("/v1") else f"{base}/v1{path}"

    def _image_block(self, path: str) -> dict[str, Any] | None:
        got = image_b64(path)
        if not got:
            return None
        mime, data = got
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}

    def convert(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def push(role: str, blocks: list[dict[str, Any]]) -> None:
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})

        for m in messages:
            role = m["role"]
            if role == "user":
                blocks: list[dict[str, Any]] = [{"type": "text", "text": m.get("content") or "(空)"}]
                if self.vision:
                    blocks += [b for b in (self._image_block(p) for p in m.get("images") or []) if b]
                push("user", blocks)
            elif role == "assistant":
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls") or []:
                    try:
                        args = json.loads(tc.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {"_raw": tc.get("arguments")}
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": args})
                if blocks:
                    push("assistant", blocks)
            elif role == "tool":
                content: list[dict[str, Any]] = [{"type": "text", "text": m.get("content") or "(空)"}]
                if self.vision:
                    content += [b for b in (self._image_block(p) for p in m.get("images") or []) if b]
                push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": content}])
        return out

    async def stream(self, system, messages, tools, emit: Emit) -> TurnResult:
        if not self.api_key:
            raise ProviderError("未配置 Anthropic API Key")
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": self.convert(messages),
            "stream": True,
        }
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                             for t in tools]
        result = TurnResult()
        blocks: dict[int, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            async with client.stream("POST", self._url("/messages"), headers=self._headers(), json=body) as resp:
                await raise_for_status(resp, self.model)
                async for _event, data in iter_sse(resp):
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    t = ev.get("type")
                    if t == "content_block_start":
                        cb = ev.get("content_block") or {}
                        blocks[ev["index"]] = {"type": cb.get("type"), "id": cb.get("id"), "name": cb.get("name"),
                                               "json": ""}
                    elif t == "content_block_delta":
                        d = ev.get("delta") or {}
                        dt = d.get("type")
                        if dt == "text_delta":
                            result.text += d.get("text", "")
                            await emit({"type": "text_delta", "delta": d.get("text", "")})
                        elif dt == "thinking_delta":
                            result.reasoning += d.get("thinking", "")
                            await emit({"type": "reasoning_delta", "delta": d.get("thinking", "")})
                        elif dt == "input_json_delta":
                            blocks.setdefault(ev["index"], {"json": ""})["json"] += d.get("partial_json", "")
                    elif t == "message_delta":
                        if (ev.get("delta") or {}).get("stop_reason") == "max_tokens":
                            await emit({"type": "notice", "text": "回复达到 max_tokens 上限被截断"})
                        if ev.get("usage"):
                            result.usage = ev["usage"]
                    elif t == "error":
                        err = ev.get("error") or {}
                        raise ProviderError(f"Claude 返回错误：{err.get('message', err)}")
        for idx in sorted(blocks):
            b = blocks[idx]
            if b.get("type") == "tool_use":
                result.tool_calls.append(ToolCall(id=b["id"], name=b["name"], arguments=b["json"] or "{}"))
        return result

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(self._url("/models"), headers=self._headers())
            if r.status_code >= 400:
                raise ProviderError(f"获取模型列表失败 (HTTP {r.status_code}): {r.text[:300]}")
            return [m["id"] for m in r.json().get("data", []) if m.get("id")]
