"""Text-to-speech engines.

- edge:       Microsoft neural voices used by countless short videos (free; needs a reachable region or a proxy)
- volcengine: ByteDance 豆包语音 / 火山引擎 — the voice family behind 剪映/抖音 dubbing (trial quota, then paid)
- gptsovits:  local GPT-SoVITS server — clone any voice from a few seconds of reference audio (free, local)
- api:        any OpenAI-compatible /audio/speech (OpenAI, SiliconFlow CosyVoice, local CosyVoice/IndexTTS/Kokoro wrappers)
- local:      offline Kokoro neural voices via sherpa-onnx (free; one-time ~150 MB model download) — see local_tts.py
- system:     the OS's built-in offline voice (last resort; refuses when no voice speaks the text's language)
"""
from __future__ import annotations

import asyncio
import base64
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from . import ffmpeg as ff
from . import local_tts
from . import subs as sublib

PUNCT = "，。！？,.!?；;、：:…"
VOLC_URL = "https://openspeech.bytedance.com/api/v1/tts"
EDGE_COOLDOWN = 600  # after Edge fails, skip it in "auto" mode for 10 minutes instead of retrying every call
_edge_failed_at = 0.0

# Edge "personas": popular short-video voice styles mapped to Edge neural voices (+ default speed/pitch).
EDGE_PERSONAS: dict[str, dict[str, str]] = {
    "解说男声": {"voice": "zh-CN-YunxiNeural", "rate": "+8%", "pitch": "+0Hz", "desc": "云希 · 影视解说/“小帅小美”解说风格，年轻有磁性"},
    "热血解说": {"voice": "zh-CN-YunjianNeural", "rate": "+6%", "pitch": "+0Hz", "desc": "云健 · 体育/游戏/激情解说"},
    "新闻主播": {"voice": "zh-CN-YunyangNeural", "rate": "+0%", "pitch": "+0Hz", "desc": "云扬 · 专业播音腔，知识科普"},
    "温柔女声": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%", "pitch": "+0Hz", "desc": "晓晓 · 温暖自然，vlog/情感/种草"},
    "元气少女": {"voice": "zh-CN-XiaoyiNeural", "rate": "+6%", "pitch": "+2Hz", "desc": "晓伊 · 活泼可爱"},
    "可爱童声": {"voice": "zh-CN-YunxiaNeural", "rate": "+0%", "pitch": "+0Hz", "desc": "云夏 · 男童"},
    "东北老妹": {"voice": "zh-CN-liaoning-XiaobeiNeural", "rate": "+0%", "pitch": "+0Hz", "desc": "晓北 · 东北话，搞笑段子"},
    "陕西女娃": {"voice": "zh-CN-shaanxi-XiaoniNeural", "rate": "+0%", "pitch": "+0Hz", "desc": "晓妮 · 陕西话"},
    "粤语女声": {"voice": "zh-HK-HiuMaanNeural", "rate": "+0%", "pitch": "+0Hz", "desc": "曉曼 · 粤语"},
    "台湾腔": {"voice": "zh-TW-HsiaoChenNeural", "rate": "+0%", "pitch": "+0Hz", "desc": "曉臻 · 台湾国语"},
    "英文男声": {"voice": "en-US-AndrewMultilingualNeural", "rate": "+0%", "pitch": "+0Hz", "desc": "Andrew · 温暖自然，可读多语种"},
    "英文女声": {"voice": "en-US-AvaMultilingualNeural", "rate": "+0%", "pitch": "+0Hz", "desc": "Ava · 清晰亲和，可读多语种"},
}


class TTSError(RuntimeError):
    pass


def split_text(text: str, max_chars: int = 250) -> list[str]:
    """Split at sentence punctuation so long scripts can go to APIs with per-request limits."""
    parts = [p for p in re.split(r"(?<=[。！？!?；;\n])", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in parts:
        while len(p) > max_chars:
            cut = max((p.rfind(c, 0, max_chars) for c in "，,、 "), default=-1)
            cut = cut + 1 if cut > max_chars // 3 else max_chars
            chunks.append(p[:cut])
            p = p[cut:]
        if cur and len(cur) + len(p) > max_chars:
            chunks.append(cur)
            cur = ""
        cur += p
    if cur.strip():
        chunks.append(cur)
    return [c.strip() for c in chunks if c.strip()]


async def concat_audio(files: list[Path], out: Path) -> None:
    if len(files) == 1:
        await ff.ffmpeg(["-i", str(files[0]), "-c:a", "libmp3lame", "-q:a", "3", str(out)])
        return
    lst = files[0].parent / f"concat_{uuid.uuid4().hex[:6]}.txt"
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), "utf-8")
    await ff.ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-c:a", "libmp3lame", "-q:a", "3", str(out)])


# ---- engines ------------------------------------------------------------------

async def edge(text: str, out: Path, voice: str, rate: str, pitch: str, proxy: str | None = None) -> list[dict[str, Any]]:
    """Returns word timings when available."""
    try:
        import edge_tts  # type: ignore
    except ImportError as e:
        raise TTSError("未安装 edge-tts（pip install edge-tts）") from e
    last: Exception | None = None
    for attempt in range(3):
        words: list[dict[str, Any]] = []
        try:
            kwargs: dict[str, Any] = {"rate": rate, "pitch": pitch}
            if proxy:
                kwargs["proxy"] = proxy
            try:
                comm = edge_tts.Communicate(text, voice, boundary="WordBoundary", **kwargs)
            except TypeError:
                comm = edge_tts.Communicate(text, voice, **kwargs)
            with open(out, "wb") as f:
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        st = chunk["offset"] / 1e7
                        words.append({"start": round(st, 3), "end": round(st + chunk["duration"] / 1e7, 3),
                                      "word": chunk["text"]})
            if out.stat().st_size > 1000:
                return words
            last = TTSError("没有返回音频")
        except Exception as e:  # noqa: BLE001 — network / service errors
            last = e
        await asyncio.sleep(0.8 * (attempt + 1))
    if "403" in str(last):
        raise TTSError("Edge 配音被微软拒绝（403）。微软自 2026-10 起在部分地区停用了免费接口，北美网络正常；"
                       "可在 设置 → 语音 填写代理，或改用 火山引擎 / 声音克隆")
    msg = re.sub(r"https?://\S+|wss?://\S+", "", str(last)).strip(" ,'")
    raise TTSError(f"Edge 配音不可用：{msg[:120]}")


async def volcengine(text: str, out: Path, appid: str, token: str, voice_type: str, speed: float = 1.0,
                     cluster: str = "volcano_tts") -> list[tuple[str, float]]:
    """豆包语音 / 火山引擎 HTTP V1. Returns [(chunk_text, seconds)] for subtitle timing."""
    if not appid or not token or not voice_type:
        raise TTSError("未配置火山引擎（需要 AppID、Access Token 和音色 voice_type）")
    tmp = Path(tempfile.mkdtemp(prefix="volc_"))
    files, timing = [], []
    async with httpx.AsyncClient(timeout=60) as client:
        for i, chunk in enumerate(split_text(text, 280)):
            body = {
                "app": {"appid": appid, "token": "access_token", "cluster": cluster},
                "user": {"uid": "autostudio"},
                "audio": {"voice_type": voice_type, "encoding": "mp3", "speed_ratio": speed},
                "request": {"reqid": uuid.uuid4().hex, "text": chunk, "operation": "query"},
            }
            r = await client.post(VOLC_URL, json=body,
                                  headers={"Authorization": f"Bearer;{token}"})
            try:
                j = r.json()
            except ValueError as e:
                raise TTSError(f"火山引擎返回异常 (HTTP {r.status_code})：{r.text[:200]}") from e
            if j.get("code") != 3000 or not j.get("data"):
                raise TTSError(f"火山引擎合成失败（code {j.get('code')}）：{j.get('message')}")
            f = tmp / f"{i:03d}.mp3"
            f.write_bytes(base64.b64decode(j["data"]))
            files.append(f)
            dur = float((j.get("addition") or {}).get("duration") or 0) / 1000
            timing.append((chunk, dur))
    await concat_audio(files, out)
    return timing


async def gptsovits(text: str, out: Path, base_url: str, ref_audio: str, prompt_text: str, prompt_lang: str = "zh",
                    text_lang: str = "zh", speed: float = 1.0) -> None:
    """GPT-SoVITS api_v2 (python api_v2.py), default http://127.0.0.1:9880."""
    if not base_url or not ref_audio:
        raise TTSError("未配置声音克隆（需要 GPT-SoVITS 服务地址和参考音频）")
    body = {"text": text, "text_lang": text_lang, "ref_audio_path": ref_audio, "prompt_text": prompt_text,
            "prompt_lang": prompt_lang, "text_split_method": "cut5", "batch_size": 1, "media_type": "wav",
            "streaming_mode": False, "speed_factor": speed}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
            r = await client.post(f"{base_url.rstrip('/')}/tts", json=body)
    except httpx.ConnectError as e:
        raise TTSError(f"连不上 GPT-SoVITS 服务 {base_url}（请先启动 api_v2.py）") from e
    if r.status_code >= 400 or not r.content or r.headers.get("content-type", "").startswith("application/json"):
        raise TTSError(f"GPT-SoVITS 合成失败 (HTTP {r.status_code})：{r.text[:200]}")
    wav = out.with_suffix(".src.wav")
    wav.write_bytes(r.content)
    try:
        await ff.ffmpeg(["-i", str(wav), "-c:a", "libmp3lame", "-q:a", "3", str(out)])
    finally:
        wav.unlink(missing_ok=True)


async def openai_compatible(text: str, out: Path, base_url: str, api_key: str, model: str, voice: str,
                            speed: float = 1.0) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20)) as client:
        r = await client.post(f"{base_url.rstrip('/')}/audio/speech",
                              headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                              json={"model": model, "input": text, "voice": voice, "response_format": "mp3",
                                    "speed": speed})
    if r.status_code >= 400:
        raise TTSError(f"语音合成 API 失败 (HTTP {r.status_code})：{r.text[:300]}")
    out.write_bytes(r.content)


async def system_voice(text: str, out: Path, lang_hint: str = "zh") -> str:
    """Offline OS voice (Windows SAPI / macOS say / Linux espeak-ng). Returns the voice used."""
    tmpdir = Path(tempfile.mkdtemp(prefix="tts_"))
    txt = tmpdir / "text.txt"
    txt.write_text(text, "utf-8")
    wav = tmpdir / "out.wav"
    if ff.IS_WIN:
        script = (
            "Add-Type -AssemblyName System.Speech;"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"$v=$s.GetInstalledVoices()|?{{$_.VoiceInfo.Culture.Name -like '{lang_hint}*'}}|select -First 1;"
            "if(-not $v){[Console]::Error.WriteLine('NOVOICE');exit 3};$s.SelectVoice($v.VoiceInfo.Name);"
            f"$s.SetOutputToWaveFile('{wav}');"
            f"$s.Speak([IO.File]::ReadAllText('{txt}',[Text.Encoding]::UTF8));"
            "$n=$s.Voice.Name;$s.Dispose();Write-Output $n"
        )
        code, voice, err = await ff.run_proc(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=600)
    elif ff.IS_MAC:
        v = {"zh": "Tingting", "ja": "Kyoko", "ko": "Yuna"}.get(lang_hint, "Samantha")
        aiff = tmpdir / "out.aiff"
        code, _o, err = await ff.run_proc(["say", "-v", v, "-o", str(aiff), "-f", str(txt)], timeout=600)
        voice = v
        if code == 0:
            await ff.ffmpeg(["-i", str(aiff), str(wav)])
    else:
        voice = f"espeak-ng ({lang_hint})"
        code, _o, err = await ff.run_proc(["espeak-ng", "-v", lang_hint, "-w", str(wav), "-f", str(txt)], timeout=600)
    if "NOVOICE" in (err or ""):
        raise TTSError(f"系统没有安装{'中文' if lang_hint == 'zh' else lang_hint}语音")
    if code != 0 or not wav.exists():
        raise TTSError(f"系统语音合成失败：{err[-300:]}")
    await ff.ffmpeg(["-i", str(wav), "-c:a", "libmp3lame", "-q:a", "3", str(out)])
    return voice.strip()


# ---- orchestration -------------------------------------------------------------

def _rate_to_speed(rate: str | None) -> float:
    m = re.match(r"^([+-]?\d+)%$", rate or "")
    return round(1 + int(m.group(1)) / 100, 2) if m else 1.0


def engine_order(cfg: dict[str, Any]) -> list[str]:
    engine = cfg.get("engine") or "auto"
    if engine != "auto":
        return [engine]
    order = ["edge"]
    if (cfg.get("volc") or {}).get("appid"):
        order.append("volcengine")
    if (cfg.get("clone") or {}).get("ref_audio"):
        order.append("gptsovits")
    if cfg.get("api_provider"):
        order.append("api")
    if local_tts.available():
        order.append("local")
    return order + ["system"]


def _sentences(text: str, max_chars: int = 48) -> list[str]:
    """Short pieces for the local engine: real per-sentence durations make subtitles line up."""
    out: list[str] = []
    for part in (p.strip() for p in re.split(r"(?<=[。！？!?；;\n])", text)):
        while len(part) > max_chars:
            cut = max((part.rfind(c, 0, max_chars) for c in "，,、 "), default=-1)
            cut = cut + 1 if cut > max_chars // 3 else max_chars
            out.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            out.append(part)
    return out


async def synthesize(text: str, out: Path, settings: dict[str, Any], voice: str | None = None,
                     rate: str | None = None, pitch: str | None = None, engine: str | None = None) -> dict[str, Any]:
    """Run engines in order until one works. `voice` may be an Edge persona name, an Edge voice id,
    a 火山 voice_type, or an API voice — it is passed to whichever engine it fits."""
    global _edge_failed_at
    cfg = settings.get("tts") or {}
    order = [engine] if engine else engine_order(cfg)
    errors: list[str] = []
    if not engine and len(order) > 1 and time.time() - _edge_failed_at < EDGE_COOLDOWN and "edge" in order:
        order.remove("edge")
        errors.append("edge: 最近连接失败，暂时跳过（10 分钟后重试）")
    cjk = sublib.is_cjk_text(text)
    for eng in order:
        try:
            if eng == "edge":
                persona = EDGE_PERSONAS.get(voice or "") or EDGE_PERSONAS.get(cfg.get("persona") or "") or {}
                v = voice if voice and "Neural" in voice else persona.get("voice") or cfg.get("voice") or "zh-CN-YunxiNeural"
                r = rate or persona.get("rate") or "+0%"
                p = pitch or persona.get("pitch") or "+0Hz"
                words = await edge(text, out, v, r, p, proxy=cfg.get("proxy") or None)
                return {"engine": f"Edge · {v}", "words": words, "errors": errors}
            if eng == "volcengine":
                vc = cfg.get("volc") or {}
                vt = voice if voice and "_" in voice and "Neural" not in voice else vc.get("voice_type", "")
                timing = await volcengine(text, out, vc.get("appid", ""), vc.get("token", ""), vt,
                                          _rate_to_speed(rate), vc.get("cluster") or "volcano_tts")
                return {"engine": f"火山引擎 · {vt}", "chunks": timing, "errors": errors}
            if eng == "gptsovits":
                cl = cfg.get("clone") or {}
                await gptsovits(text, out, cl.get("base_url") or "http://127.0.0.1:9880", cl.get("ref_audio", ""),
                                cl.get("prompt_text", ""), cl.get("prompt_lang") or "zh", "zh" if cjk else "en",
                                _rate_to_speed(rate))
                return {"engine": f"声音克隆 · {Path(cl.get('ref_audio', '')).stem}", "errors": errors}
            if eng == "api":
                pcfg = (settings.get("providers") or {}).get(cfg.get("api_provider") or "") or {}
                if not pcfg.get("base_url"):
                    raise TTSError("未配置语音合成 API 提供方")
                v = voice if voice and voice not in EDGE_PERSONAS and "Neural" not in voice else (cfg.get("api_voice") or "alloy")
                await openai_compatible(text, out, pcfg["base_url"], pcfg.get("api_key", ""),
                                        cfg.get("api_model") or "gpt-4o-mini-tts", v, _rate_to_speed(rate))
                return {"engine": f"API · {v}", "errors": errors}
            if eng == "local":
                if not local_tts.available():
                    raise TTSError("未安装离线配音组件（pip install sherpa-onnx）")
                try:
                    await local_tts.ensure_model()
                except RuntimeError as e:
                    raise TTSError(str(e)) from e
                pv = local_tts.pick_voice(voice, cfg.get("persona"), cjk)
                speed = round(pv["speed"] * (_rate_to_speed(rate) if rate else 1.0), 2)
                wav = out.with_suffix(".src.wav")
                timing = await asyncio.to_thread(local_tts.synth_wav, _sentences(text), wav, pv["sid"], speed)
                try:
                    await ff.ffmpeg(["-i", str(wav), "-c:a", "libmp3lame", "-q:a", "3", str(out)])
                finally:
                    wav.unlink(missing_ok=True)
                name = next((k for k, v in local_tts.PERSONAS.items() if v["sid"] == pv["sid"]), f"音色 {pv['sid']}")
                return {"engine": f"离线配音 · {name}", "chunks": timing, "errors": errors}
            if eng == "system":
                used = await system_voice(text, out, "zh" if cjk else "en")
                return {"engine": f"系统语音 · {used}", "errors": errors}
            raise TTSError(f"未知配音引擎：{eng}")
        except TTSError as e:
            errors.append(f"{eng}: {e}")
            out.unlink(missing_ok=True)
            if eng == "edge":
                _edge_failed_at = time.time()
    raise TTSError("所有配音引擎都失败了：" + "；".join(errors))


def words_from_text(text: str, words: list[dict[str, Any]], cjk: bool) -> list[dict[str, Any]]:
    """Re-attach the original punctuation to boundary words so cues break at natural pauses."""
    pos = 0
    for wd in words:
        k = text.find(wd["word"], pos)
        if k < 0:
            continue
        end = k + len(wd["word"])
        while end < len(text) and text[end] in PUNCT:
            end += 1
        wd["word"] = text[k:end] if cjk else " " + text[k:end]
        pos = end
    return words


def estimate_cues(text: str, duration: float, max_chars: int, offset: float = 0.0) -> list[sublib.Cue]:
    """No word timings: split by punctuation and spread time proportionally (pauses weighted)."""
    pieces = [p.strip() for p in re.split(r"(?<=[，。！？,.!?；;：:…\n])", text) if p.strip()]
    chunks: list[str] = []
    for p in pieces:
        while sublib.text_len(p) > max_chars * 1.2:
            cut = max(1, int(len(p) * max_chars / max(sublib.text_len(p), 1)))
            chunks.append(p[:cut])
            p = p[cut:]
        if p:
            chunks.append(p)
    weights = [sublib.text_len(c) + (0.8 if c[-1:] in PUNCT else 0) for c in chunks]
    total = sum(weights) or 1
    t, cues = offset, []
    for c, w in zip(chunks, weights):
        d = duration * w / total
        cues.append(sublib.Cue(t, t + d, sublib.strip_punctuation(c) if sublib.is_cjk_text(c) else c))
        t += d
    return cues


def cues_for(text: str, result: dict[str, Any], duration: float, max_chars: int) -> list[sublib.Cue]:
    cjk = sublib.is_cjk_text(text)
    if result.get("words"):
        return sublib.segment_words(words_from_text(text, result["words"], cjk), max_chars=max_chars, strip_punct=cjk)
    if result.get("chunks"):  # per-request durations from chunked engines: estimate inside each chunk only
        cues: list[sublib.Cue] = []
        t = 0.0
        for chunk, dur in result["chunks"]:
            cues += estimate_cues(chunk, dur, max_chars, offset=t)
            t += dur
        return cues
    return estimate_cues(text, duration, max_chars)
