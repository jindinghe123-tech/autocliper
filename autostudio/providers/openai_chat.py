"""OpenAI-compatible Chat Completions (DeepSeek, Qwen, Kimi, GLM, Gemini, OpenRouter, Ollama, ...)."""
from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from .base import (TIMEOUT, Emit, Provider, ProviderError, ToolCall, TurnResult, image_data_url, iter_sse,
                   raise_for_status)


class OpenAIChatProvider(Provider):
    def __init__(self, base_url: str, api_key: str, model: str, vision: bool = False,
                 reasoning_echo: bool = False, extra_headers: dict[str, str] | None = None):
        if not base_url:
            raise ProviderError("未配置 Base URL")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.vision = vision
        self.reasoning_echo = reasoning_echo
        self.extra_headers = extra_headers or {}

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if "openrouter.ai" in self.base_url:
            h.setdefault("HTTP-Referer", "https://github.com/autostudio")
            h.setdefault("X-Title", "AutoStudio")
        return h

    def convert(self, system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        pending_images: list[str] = []

        def flush_images() -> None:
            if pending_images and self.vision:
                parts: list[dict[str, Any]] = [{"type": "text", "text": "（以上工具返回的图片）"}]
                for p in pending_images:
                    url = image_data_url(p)
                    if url:
                        parts.append({"type": "image_url", "image_url": {"url": url}})
                out.append({"role": "user", "content": parts})
            pending_images.clear()

        for m in messages:
            role = m["role"]
            if role != "tool":
                flush_images()
            if role == "user":
                imgs = m.get("images") or []
                if imgs and self.vision:
                    parts = [{"type": "text", "text": m.get("content") or ""}]
                    for p in imgs:
                        url = image_data_url(p)
                        if url:
                            parts.append({"type": "image_url", "image_url": {"url": url}})
                    out.append({"role": "user", "content": parts})
                else:
                    out.append({"role": "user", "content": m.get("content") or ""})
            elif role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": tc.get("arguments") or "{}"}}
                        for tc in m["tool_calls"]
                    ]
                    if not msg["content"]:
                        msg["content"] = None
                if self.reasoning_echo and m.get("reasoning_content"):
                    msg["reasoning_content"] = m["reasoning_content"]
                out.append(msg)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m.get("content") or ""})
                pending_images.extend(m.get("images") or [])
        flush_images()
        return out

    async def stream(self, system, messages, tools, emit: Emit) -> TurnResult:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self.convert(system, messages),
            "stream": True,
        }
        if tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["parameters"]}}
                for t in tools
            ]
        result = TurnResult()
        calls: dict[int, dict[str, str]] = {}
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            async with client.stream("POST", f"{self.base_url}/chat/completions", headers=self._headers(),
                                     json=body) as resp:
                await raise_for_status(resp, self.model)
                async for _event, data in iter_sse(resp):
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        err = chunk["error"]
                        raise ProviderError(err.get("message") if isinstance(err, dict) else str(err))
                    if chunk.get("usage"):
                        result.usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        rc = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(rc, str) and rc:
                            result.reasoning += rc
                            result.reasoning_content += rc
                            await emit({"type": "reasoning_delta", "delta": rc})
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            result.text += text
                            await emit({"type": "text_delta", "delta": text})
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", len(calls))
                            slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                args = fn["arguments"]
                                slot["arguments"] += args if isinstance(args, str) else json.dumps(args)
        for idx in sorted(calls):
            c = calls[idx]
            if not c["name"]:
                continue
            result.tool_calls.append(ToolCall(id=c["id"] or f"call_{uuid.uuid4().hex[:12]}", name=c["name"],
                                              arguments=c["arguments"] or "{}"))
        return result

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{self.base_url}/models", headers=self._headers())
            if r.status_code >= 400:
                raise ProviderError(f"获取模型列表失败 (HTTP {r.status_code}): {r.text[:300]}")
            data = r.json()
        from .catalog import sort_and_filter

        items = data.get("data", data) if isinstance(data, dict) else data
        return sort_and_filter(items or [])
