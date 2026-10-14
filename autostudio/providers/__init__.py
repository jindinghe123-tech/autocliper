"""Provider factory."""
from __future__ import annotations

from typing import Any

from .anthropic import AnthropicProvider
from .base import Provider, ProviderError, stream_retrying
from .openai_chat import OpenAIChatProvider
from .responses import ResponsesProvider


def make_provider(settings: dict[str, Any], provider_id: str | None = None, model: str | None = None,
                  session_id: str | None = None) -> Provider:
    active = settings.get("active") or {}
    pid = provider_id or active.get("provider") or "chatgpt"
    cfg = (settings.get("providers") or {}).get(pid)
    if not cfg:
        raise ProviderError(f"找不到模型提供方「{pid}」，请在设置中配置。")
    model = model or (active.get("model") if pid == active.get("provider") else None) or (cfg.get("models") or [""])[0]
    if not model:
        raise ProviderError(f"「{cfg.get('name', pid)}」没有设置模型名称。")
    ptype = cfg.get("type")
    vision = bool(cfg.get("vision", False))
    if ptype == "chatgpt":
        from .catalog import model_meta

        meta = model_meta(model)
        if meta is not None:
            vision = bool(meta.get("vision", vision))
        return ResponsesProvider(model=model, chatgpt=True, reasoning_effort=cfg.get("reasoning_effort") or "auto",
                                 vision=vision, session_id=session_id)
    if ptype == "openai_responses":
        return ResponsesProvider(model=model, base_url=cfg.get("base_url") or "https://api.openai.com/v1",
                                 api_key=cfg.get("api_key", ""), reasoning_effort=cfg.get("reasoning_effort"),
                                 vision=vision)
    if ptype == "anthropic":
        return AnthropicProvider(api_key=cfg.get("api_key", ""), model=model,
                                 base_url=cfg.get("base_url") or "https://api.anthropic.com", vision=vision)
    if ptype == "openai_chat":
        return OpenAIChatProvider(base_url=cfg.get("base_url", ""), api_key=cfg.get("api_key", ""), model=model,
                                  vision=vision, reasoning_echo=bool(cfg.get("reasoning_echo")))
    raise ProviderError(f"未知的提供方类型：{ptype}")


__all__ = ["make_provider", "Provider", "ProviderError", "stream_retrying"]
