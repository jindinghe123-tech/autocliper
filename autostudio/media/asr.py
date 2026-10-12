"""Speech recognition: local faster-whisper, or any OpenAI-compatible /audio/transcriptions endpoint."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable

import httpx

_models: dict[tuple[str, str, str], Any] = {}
_lock = threading.Lock()

ZH_PROMPT = "以下是普通话的句子，使用简体中文和标点符号。"


def faster_whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


_cuda_broken = False
CUDA_ERR = ("cublas", "cudnn", "cuda", "libcudart")


def _resolve_device(device: str) -> str:
    if device != "auto":
        return device
    if _cuda_broken:
        return "cpu"
    try:
        import ctranslate2  # type: ignore

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def _load_model(size: str, device: str):
    from faster_whisper import WhisperModel  # type: ignore

    compute = "float16" if device == "cuda" else "int8"
    key = (size, device, compute)
    with _lock:
        if key not in _models:
            _models[key] = WhisperModel(size, device=device, compute_type=compute)
        return _models[key]


def _load_wav(path: str):
    """16 kHz mono s16le WAV (as produced by our ffmpeg step) -> float32 array.

    Passing samples directly avoids faster-whisper's PyAV decoding path, which breaks across PyAV versions.
    """
    import wave

    import numpy as np

    with wave.open(path, "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("音频需为 16kHz 单声道 16bit WAV")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe_local(audio: str, *, model: str = "small", device: str = "auto", language: str | None = None,
                     prompt: str | None = None, hf_endpoint: str = "",
                     progress: Callable[[float, str], None] | None = None,
                     cancel: threading.Event | None = None) -> dict[str, Any]:
    if hf_endpoint:
        os.environ.setdefault("HF_ENDPOINT", hf_endpoint)
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    if progress:
        progress(0.0, f"加载语音模型 {model}（首次使用需要下载）…")
    lang = None if not language or language == "auto" else language
    samples = _load_wav(audio)
    dev = _resolve_device(device)
    try:
        return _transcribe(_load_model(model, dev), samples, lang, prompt, progress, cancel)
    except RuntimeError as e:
        # GPU present but CUDA/cuBLAS runtime missing: fall back to CPU (only when the user chose "auto").
        global _cuda_broken
        if dev == "cuda" and device == "auto" and any(k in str(e).lower() for k in CUDA_ERR):
            _cuda_broken = True
            if progress:
                progress(0.0, "未找到 CUDA 运行库，改用 CPU 识别…")
            return _transcribe(_load_model(model, "cpu"), samples, lang, prompt, progress, cancel)
        raise


def _transcribe(m, samples, lang, prompt, progress, cancel) -> dict[str, Any]:
    def run(initial_prompt: str | None):
        return m.transcribe(samples, language=lang, task="transcribe", vad_filter=True, word_timestamps=True,
                            beam_size=5, initial_prompt=initial_prompt, condition_on_previous_text=False)

    segments, info = run(prompt or (ZH_PROMPT if lang == "zh" else None))
    if lang is None and info.language == "zh" and not prompt:
        segments, info = run(ZH_PROMPT)  # nudge towards simplified Chinese with punctuation
    duration = float(info.duration or 0) or 1.0
    words: list[dict[str, Any]] = []
    segs: list[dict[str, Any]] = []
    for seg in segments:
        if cancel and cancel.is_set():
            raise RuntimeError("已取消")
        segs.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
        for w in seg.words or []:
            words.append({"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word})
        if progress:
            progress(min(0.99, seg.end / duration), seg.text.strip()[:40])
    return {"language": info.language, "duration": info.duration, "segments": segs, "words": words}


async def transcribe_api(audio: str, *, base_url: str, api_key: str, model: str = "whisper-1",
                         language: str | None = None, prompt: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"model": model, "response_format": "verbose_json"}
    if language and language != "auto":
        data["language"] = language
    if prompt:
        data["prompt"] = prompt
    files = [("timestamp_granularities[]", (None, "word")), ("timestamp_granularities[]", (None, "segment"))]
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30)) as client:
        with open(audio, "rb") as f:
            r = await client.post(f"{base_url.rstrip('/')}/audio/transcriptions",
                                  headers={"Authorization": f"Bearer {api_key}"},
                                  data=data, files=[("file", (Path(audio).name, f, "audio/mpeg")), *files])
    if r.status_code >= 400:
        raise RuntimeError(f"语音识别 API 失败 (HTTP {r.status_code})：{r.text[:400]}")
    j = r.json()
    words = [{"start": w["start"], "end": w["end"], "word": (" " + w["word"]) if not w["word"].startswith(" ") else w["word"]}
             for w in j.get("words") or []]
    segs = [{"start": s["start"], "end": s["end"], "text": s.get("text", "").strip()} for s in j.get("segments") or []]
    if not segs and j.get("text"):
        segs = [{"start": 0.0, "end": float(j.get("duration") or 0), "text": j["text"]}]
    lang = j.get("language")
    if words and lang in ("chinese", "zh", "japanese", "ja"):
        for w in words:
            w["word"] = w["word"].strip()
    return {"language": lang, "duration": j.get("duration"), "segments": segs, "words": words}
