"""Offline neural voices (Kokoro v1.1 multi-lang via sherpa-onnx): free, no network after a one-time
~150 MB model download, natural Mandarin (55 female + 45 male voices) and English.

This is the dependable fallback for when Microsoft's free Edge voices are blocked in the user's region
and the OS has no Chinese voice installed.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tarfile
import threading
import urllib.request
import wave
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import DATA_DIR

MODEL_NAME = "kokoro-int8-multi-lang-v1_1"
MODEL_DIR = DATA_DIR / "models" / MODEL_NAME
TARBALL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{MODEL_NAME}.tar.bz2"
HF_REPO = f"csukuangfj/{MODEL_NAME}"
HF_HOSTS = ["https://huggingface.co", "https://hf-mirror.com"]
REQUIRED = ["model.int8.onnx", "voices.bin", "tokens.txt", "lexicon-zh.txt", "lexicon-us-en.txt", "dict", "espeak-ng-data"]

# Speaker ids (sid): 0-2 English female, 3-57 Mandarin female, 58-102 Mandarin male.
PERSONAS: dict[str, dict[str, Any]] = {
    "解说男声": {"sid": 72, "speed": 1.08}, "热血解说": {"sid": 60, "speed": 1.12}, "新闻主播": {"sid": 63, "speed": 1.0},
    "温柔女声": {"sid": 12, "speed": 1.0}, "元气少女": {"sid": 40, "speed": 1.08}, "可爱童声": {"sid": 44, "speed": 1.05},
    "英文男声": {"sid": 0, "speed": 1.0}, "英文女声": {"sid": 1, "speed": 1.0},
}
DEFAULT_ZH, DEFAULT_EN = PERSONAS["解说男声"], {"sid": 1, "speed": 1.0}

_tts: Any = None
_lock = threading.Lock()
_download_lock = asyncio.Lock()
status: dict[str, Any] = {"downloading": False, "progress": 0.0, "error": ""}


def available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def installed() -> bool:
    return all((MODEL_DIR / f).exists() for f in REQUIRED)


def _proxy() -> str | None:
    try:
        p = urllib.request.getproxies()
        return p.get("https") or p.get("http")
    except Exception:  # noqa: BLE001
        return None


async def _get(c: httpx.AsyncClient, url: str, dest: Path, report: Callable[[int], None] | None = None) -> None:
    async with c.stream("GET", url) as r:
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            async for chunk in r.aiter_bytes(512 * 1024):
                f.write(chunk)
                if report:
                    report(len(chunk))


async def _from_tarball(c: httpx.AsyncClient, tmp: Path) -> None:
    tar = tmp / "model.tar.bz2"
    async with c.stream("GET", TARBALL) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        got = 0
        with tar.open("wb") as f:
            async for chunk in r.aiter_bytes(1024 * 1024):
                f.write(chunk)
                got += len(chunk)
                status["progress"] = round(0.95 * got / total, 3) if total else 0
    def extract() -> None:
        with tarfile.open(tar, "r:bz2") as t:
            try:
                t.extractall(tmp, filter="data")
            except TypeError:   # Python < 3.10.12 has no extraction filters
                t.extractall(tmp)

    await asyncio.to_thread(extract)
    tar.unlink()
    (tmp / MODEL_NAME).rename(tmp / "out")


async def _from_hf(c: httpx.AsyncClient, tmp: Path, host: str) -> None:
    r = await c.get(f"{host}/api/models/{HF_REPO}")
    r.raise_for_status()
    files = [s["rfilename"] for s in r.json().get("siblings", []) if not s["rfilename"].startswith(".")]
    done = 0
    sem = asyncio.Semaphore(8)

    async def one(name: str) -> None:
        nonlocal done
        async with sem:
            await _get(c, f"{host}/{HF_REPO}/resolve/main/{name}", tmp / "out" / name)
        done += 1
        status["progress"] = round(0.95 * done / len(files), 3)

    await asyncio.gather(*(one(f) for f in files))


async def ensure_model() -> Path:
    """Download the model once (GitHub release, then HuggingFace and its China mirror)."""
    if installed():
        return MODEL_DIR
    async with _download_lock:
        if installed():
            return MODEL_DIR
        status.update(downloading=True, progress=0.0, error="")
        tmp = MODEL_DIR.parent / f".{MODEL_NAME}.part"
        errors = []
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=120), follow_redirects=True, proxy=_proxy()) as c:
                for name, job in [("GitHub", lambda: _from_tarball(c, tmp))] + [
                        (h.split("//")[1], (lambda h=h: _from_hf(c, tmp, h))) for h in HF_HOSTS]:
                    shutil.rmtree(tmp, ignore_errors=True)
                    tmp.mkdir(parents=True)
                    try:
                        await job()
                        if all((tmp / "out" / f).exists() for f in REQUIRED):
                            shutil.rmtree(MODEL_DIR, ignore_errors=True)
                            (tmp / "out").rename(MODEL_DIR)
                            status["progress"] = 1.0
                            return MODEL_DIR
                        errors.append(f"{name}: 文件不完整")
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{name}: {type(e).__name__} {str(e)[:80]}")
            status["error"] = "离线配音模型下载失败：" + "；".join(errors)
            raise RuntimeError(status["error"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            status["downloading"] = False


def _engine() -> Any:
    global _tts
    if _tts is None:
        import sherpa_onnx

        m = MODEL_DIR
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(m / "model.int8.onnx"), voices=str(m / "voices.bin"), tokens=str(m / "tokens.txt"),
                    data_dir=str(m / "espeak-ng-data"), dict_dir=str(m / "dict"),
                    lexicon=f"{m / 'lexicon-us-en.txt'},{m / 'lexicon-zh.txt'}"),
                num_threads=max(2, min(8, os.cpu_count() or 4))),
            rule_fsts=",".join(str(m / f) for f in ("phone-zh.fst", "date-zh.fst", "number-zh.fst") if (m / f).exists()))
        _tts = sherpa_onnx.OfflineTts(cfg)
    return _tts


def pick_voice(voice: str | None, persona: str | None, cjk: bool) -> dict[str, Any]:
    for v in (voice, persona):
        if v in PERSONAS:
            return PERSONAS[v]
        m = re.fullmatch(r"(?:kokoro[:#-]?)?(\d{1,3})", str(v or ""))
        if m and int(m.group(1)) < 103:
            return {"sid": int(m.group(1)), "speed": 1.0}
    return DEFAULT_ZH if cjk else DEFAULT_EN


def synth_wav(sentences: list[str], wav: Path, sid: int, speed: float, gap: float = 0.12) -> list[tuple[str, float]]:
    """Blocking. One sentence at a time so subtitles get real per-sentence durations."""
    import numpy as np

    with _lock:
        tts = _engine()
        parts, timing, sr = [], [], 24000
        for s in sentences:
            a = tts.generate(s, sid=sid, speed=speed)
            sr = a.sample_rate
            x = np.asarray(a.samples, dtype=np.float32)
            pad = np.zeros(int(sr * gap), dtype=np.float32)
            parts += [x, pad]
            timing.append((s, (len(x) + len(pad)) / sr))
    pcm = (np.clip(np.concatenate(parts) if parts else np.zeros(1, np.float32), -1, 1) * 32767).astype(np.int16)
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return timing
