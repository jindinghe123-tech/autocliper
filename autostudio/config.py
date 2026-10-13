"""Settings, data directories and provider presets."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

APP_NAME = "AutoStudio"
VERSION = "0.1.0"

DATA_DIR = Path(os.environ.get("AUTOSTUDIO_HOME", Path.home() / ".autostudio")).expanduser()
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_FILE = DATA_DIR / "autostudio.db"
MEMORY_FILE = DATA_DIR / "memory.md"
CHATGPT_AUTH_FILE = DATA_DIR / "chatgpt_auth.json"
SOCIAL_DIR = DATA_DIR / "social"   # per-account browser profiles (小红书) and publish debug screenshots

# Fallback only: once signed in, the real list is fetched live from the ChatGPT/Codex model catalog
# (see providers/catalog.py), so new models show up without code changes.
CHATGPT_MODELS = ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna"]
# Defaults shipped by earlier versions; replaced on load so upgrades don't keep stale models.
LEGACY_CHATGPT_DEFAULTS = [["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2"]]

# OpenAI-compatible / native presets. `type` selects the wire protocol.
PROVIDER_PRESETS: list[dict[str, Any]] = [
    # `models` are starting points; after you enter a key, AutoStudio fetches the provider's live model list
    # (newest first). Aliases like deepseek-chat / kimi-latest always point at the provider's newest model.
    {"preset": "openai", "name": "OpenAI", "type": "openai_responses",
     "base_url": "https://api.openai.com/v1", "models": ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna"], "vision": True},
    {"preset": "anthropic", "name": "Anthropic Claude", "type": "anthropic",
     "base_url": "https://api.anthropic.com", "models": ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"], "vision": True},
    {"preset": "gemini", "name": "Google Gemini", "type": "openai_chat",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "models": ["gemini-3.8-flash", "gemini-flash-latest"], "vision": True},
    {"preset": "deepseek", "name": "DeepSeek", "type": "openai_chat",
     "base_url": "https://api.deepseek.com/v1", "models": ["deepseek-chat", "deepseek-reasoner"], "vision": False, "reasoning_echo": True},
    {"preset": "qwen", "name": "通义千问 (阿里云百炼)", "type": "openai_chat",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "models": ["qwen3.8-max", "qwen-max-latest", "qwen-plus-latest"], "vision": False},
    {"preset": "moonshot", "name": "Kimi (月之暗面)", "type": "openai_chat",
     "base_url": "https://api.moonshot.cn/v1", "models": ["kimi-latest"], "vision": False, "reasoning_echo": True},
    {"preset": "zhipu", "name": "智谱 GLM", "type": "openai_chat",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "models": ["glm-5.3"], "vision": False},
    {"preset": "doubao", "name": "豆包 (火山方舟)", "type": "openai_chat",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3", "models": [], "vision": False},
    {"preset": "siliconflow", "name": "硅基流动 SiliconFlow", "type": "openai_chat",
     "base_url": "https://api.siliconflow.cn/v1", "models": [], "vision": False},
    {"preset": "openrouter", "name": "OpenRouter", "type": "openai_chat",
     "base_url": "https://openrouter.ai/api/v1", "models": ["openai/gpt-6-astra", "openrouter/auto"], "vision": True},
    {"preset": "ollama", "name": "Ollama (本地)", "type": "openai_chat",
     "base_url": "http://localhost:11434/v1", "models": [], "vision": False},
    {"preset": "custom", "name": "自定义 (OpenAI 兼容)", "type": "openai_chat",
     "base_url": "", "models": [], "vision": False},
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "active": {"provider": "chatgpt", "model": CHATGPT_MODELS[0]},
    "providers": {
        "chatgpt": {
            "type": "chatgpt",
            "name": "ChatGPT 订阅",
            "models": CHATGPT_MODELS,
            "reasoning_effort": "auto",
            "vision": True,
        }
    },
    "workspace": str(Path.home() / "AutoStudio"),
    "asr": {
        "engine": "local",          # local | api
        "model": "small",           # tiny/base/small/medium/large-v3/large-v3-turbo
        "device": "auto",           # auto/cpu/cuda
        "hf_endpoint": "",          # e.g. https://hf-mirror.com for users in mainland China
        "api_provider": "",         # provider id for engine=api (OpenAI-compatible /audio/transcriptions)
        "api_model": "whisper-1",
    },
    "tts": {
        "engine": "auto",            # auto | edge | volcengine | gptsovits | api | system
        "persona": "解说男声",        # Edge persona (see media/tts.py EDGE_PERSONAS)
        "voice": "",                 # explicit Edge voice id overrides the persona
        "proxy": "",                 # e.g. http://127.0.0.1:7890 — Edge TTS is region-blocked in some places
        "volc": {"appid": "", "token": "", "voice_type": "", "cluster": "volcano_tts"},
        "clone": {"base_url": "http://127.0.0.1:9880", "ref_audio": "", "prompt_text": "", "prompt_lang": "zh"},
        "api_provider": "",
        "api_model": "gpt-4o-mini-tts",
        "api_voice": "alloy",
    },
    "permissions": {"shell": "ask", "publish": "ask"},   # ask | allow | deny
    # Royalty-free asset search (media/stock.py). Keys are optional: Openverse + Wikimedia work without any.
    "assets": {"pexels_key": "", "pixabay_key": "", "freesound_key": "", "jamendo_client_id": "",
               "commercial_only": True, "proxy": ""},
    "social": {
        # Your own Google Cloud OAuth client (type "Desktop app"); see the guide on the 发布 page.
        "youtube": {"client_id": "", "client_secret": ""},
        "xhs_show_browser": True,    # show the browser window while publishing to 小红书 (more reliable)
    },
    "agent": {"max_steps": 60, "extra_prompt": ""},
    "ffmpeg_path": "",
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "providers":
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _hint(secret: str) -> str:
    return (secret[:3] + "…" + secret[-4:]) if len(secret) > 10 else ("已设置" if secret else "")


class Settings:
    def __init__(self, path: Path = SETTINGS_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        data: dict[str, Any] = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text("utf-8"))
            except Exception:
                data = {}
        merged = _deep_merge(DEFAULT_SETTINGS, data)
        merged.setdefault("providers", {})
        merged["providers"].setdefault("chatgpt", copy.deepcopy(DEFAULT_SETTINGS["providers"]["chatgpt"]))
        gpt = merged["providers"]["chatgpt"]
        if gpt.get("models") in LEGACY_CHATGPT_DEFAULTS:
            stale = set(gpt["models"])
            gpt["models"] = list(CHATGPT_MODELS)
            if merged.get("active", {}).get("provider") == "chatgpt" and merged["active"].get("model") in stale:
                merged["active"]["model"] = CHATGPT_MODELS[0]
        self._data = merged

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.path)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            providers_patch = patch.pop("providers", None)
            self._data = _deep_merge(self._data, patch)
            if providers_patch is not None:
                current = self._data.setdefault("providers", {})
                for pid, p in providers_patch.items():
                    if p is None:
                        if pid != "chatgpt":
                            current.pop(pid, None)
                        continue
                    old = current.get(pid, {})
                    new = {**old, **p}
                    current[pid] = new
            self.save()
            return copy.deepcopy(self._data)

    @property
    def workspace(self) -> Path:
        ws = Path(self._data.get("workspace") or (Path.home() / "AutoStudio")).expanduser()
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def public(self) -> dict[str, Any]:
        """Settings safe to send to the browser (secrets masked)."""
        data = self.get()
        for p in data.get("providers", {}).values():
            key = p.pop("api_key", "") or ""
            p["api_key_set"] = bool(key)
            p["api_key_hint"] = _hint(key)
        volc = (data.get("tts") or {}).get("volc") or {}
        token = volc.pop("token", "") or ""
        volc["token_set"], volc["token_hint"] = bool(token), _hint(token)
        yt = (data.get("social") or {}).get("youtube") or {}
        secret = yt.pop("client_secret", "") or ""
        yt["client_secret_set"] = bool(secret)
        assets = data.get("assets") or {}
        for k in ("pexels_key", "pixabay_key", "freesound_key", "jamendo_client_id"):
            v = assets.pop(k, "") or ""
            assets[f"{k}_set"], assets[f"{k}_hint"] = bool(v), _hint(v)
        return data
