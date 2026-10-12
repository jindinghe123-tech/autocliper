"""Live model discovery, so model lists never go stale.

- ChatGPT subscription: the same catalog endpoint Codex uses (GET {codex}/models?client_version=…),
  filtered to models visible for the signed-in plan, sorted by OpenAI's priority.
- API providers: their /models endpoint, chat models only, newest first.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from ..config import DATA_DIR
from .chatgpt_auth import chatgpt_auth

CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
CODEX_CLIENT_VERSION = "0.162.1"  # fallback; refreshed from GitHub releases (the catalog hides models newer clients need)
CATALOG_FILE = DATA_DIR / "chatgpt_models.json"
VERSION_FILE = DATA_DIR / "codex_version.json"
CATALOG_TTL = 300

_mem: dict[str, Any] = {"at": 0.0, "models": None}

# Non-chat models that OpenAI-compatible /models endpoints commonly include.
NON_CHAT = re.compile(
    r"embed|embedding|tts|whisper|transcri|dall-?e|image|moderation|audio|realtime|speech|sora|babbage|davinci|"
    r"rerank|bge-|text-search|similarity|guard|ocr|video|vision-preview|-instruct$|computer-use",
    re.I,
)


def _ver_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


async def codex_client_version() -> str:
    """Latest Codex CLI version (cached for a day), never lower than our built-in fallback."""
    try:
        if VERSION_FILE.exists():
            d = json.loads(VERSION_FILE.read_text("utf-8"))
            if time.time() - d.get("at", 0) < 86400:
                return max(d["version"], CODEX_CLIENT_VERSION, key=_ver_tuple)
    except Exception:
        pass
    version = CODEX_CLIENT_VERSION
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get("https://api.github.com/repos/openai/codex/releases/latest",
                            headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            tag = str(r.json().get("tag_name", ""))
            m = re.search(r"(\d+\.\d+\.\d+)", tag)
            if m:
                version = max(m.group(1), CODEX_CLIENT_VERSION, key=_ver_tuple)
    except httpx.HTTPError:
        pass
    try:
        VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        VERSION_FILE.write_text(json.dumps({"version": version, "at": time.time()}), "utf-8")
    except OSError:
        pass
    return version


def _simplify(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": m.get("slug"),
        "display_name": m.get("display_name") or m.get("slug"),
        "description": m.get("description") or "",
        "default_effort": m.get("default_reasoning_level"),
        "efforts": [x.get("effort") for x in m.get("supported_reasoning_levels") or [] if x.get("effort")],
        "vision": "image" in (m.get("input_modalities") or ["text", "image"]),
        "context_window": m.get("context_window"),
    }


def _load_file() -> list[dict[str, Any]]:
    try:
        return json.loads(CATALOG_FILE.read_text("utf-8")).get("models", [])
    except Exception:
        return []


def cached_catalog() -> list[dict[str, Any]]:
    """Last known catalog (simplified), without hitting the network."""
    return [_simplify(m) for m in _load_file()]


def model_meta(slug: str) -> dict[str, Any] | None:
    for m in _load_file():
        if m.get("slug") == slug:
            return _simplify(m)
    return None


def base_instructions(slug: str) -> str | None:
    for m in _load_file():
        if m.get("slug") == slug and m.get("base_instructions"):
            return str(m["base_instructions"])
    return None


async def fetch_chatgpt_catalog(force: bool = False) -> list[dict[str, Any]]:
    if not force and _mem["models"] is not None and time.time() - _mem["at"] < CATALOG_TTL:
        return _mem["models"]
    version = await codex_client_version()
    for attempt in range(2):
        token, account = await chatgpt_auth.credentials(force_refresh=attempt > 0)
        headers = {"Authorization": f"Bearer {token}", "chatgpt-account-id": account, "originator": "codex_cli_rs",
                   "version": version, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{CHATGPT_BASE}/models", params={"client_version": version}, headers=headers)
        if r.status_code == 401 and attempt == 0:
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"获取 ChatGPT 模型列表失败 (HTTP {r.status_code})：{r.text[:200]}")
        break
    raw = r.json().get("models") or []
    plan = (chatgpt_auth.status().get("plan") or "").lower()
    visible = []
    for m in raw:
        if m.get("visibility") not in (None, "list") or not m.get("slug"):
            continue
        plans = [str(p).lower() for p in m.get("available_in_plans") or []]
        if plans and plan and plan not in plans:
            continue
        visible.append(m)
    visible.sort(key=lambda m: (m.get("priority") is None, m.get("priority") or 0))
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_FILE.write_text(json.dumps({"at": time.time(), "models": visible}, ensure_ascii=False), "utf-8")
    simplified = [_simplify(m) for m in visible]
    _mem.update(at=time.time(), models=simplified)
    return simplified


def sort_and_filter(items: list[dict[str, Any]]) -> list[str]:
    """OpenAI-style /models items -> chat model ids, newest first."""
    rows = []
    for it in items:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        mid = str(it["id"]).removeprefix("models/")
        if NON_CHAT.search(mid):
            continue
        created = it.get("created") or it.get("created_at") or 0
        if isinstance(created, str):
            created = 0
        rows.append((float(created), mid))
    if any(c for c, _ in rows):
        rows.sort(key=lambda x: -x[0])
    return list(dict.fromkeys(mid for _, mid in rows))
