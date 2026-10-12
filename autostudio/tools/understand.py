"""Video understanding: shot structure + motion + audio + speech, dense "watching" of segments,
and native video understanding through Gemini when a key is configured.

GPT/Claude models only accept images, so (like Codex) we "watch" by sampling frames densely with timestamps —
but we also give the model what frames alone can't: where the cuts are, how much is moving, how loud it is,
and exactly what is being said.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from pathlib import Path
from typing import Any

import httpx

from ..media import ffmpeg as ff
from ..media import subs as sublib
from ..media.ffmpeg import parse_time
from .registry import ToolContext, ToolError, boolean, integer, num, obj, s, time_s, tool
from .video import drawtext

MOTION_LEVELS = [(1.0, "静止"), (4.5, "平缓"), (12.0, "中等"), (1e9, "剧烈")]  # mean abs frame diff (0..255)


def _motion_label(v: float) -> str:
    return next(label for limit, label in MOTION_LEVELS if v < limit)


def _loud_label(db: float | None) -> str:
    if db is None:
        return "无声"
    if db < -45:
        return "静音"
    if db < -30:
        return "安静"
    if db < -18:
        return "正常"
    return "响亮"


def _clock(t: float) -> str:
    m, s_ = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s_:04.1f}"


async def motion_profile(src: Path, duration: float, fps: float = 4.0,
                         progress: Any = None) -> list[tuple[float, float, float]]:
    """[(time, cut_score, motion)] sampled at `fps`.

    cut_score: ffmpeg scene score (0..1) — spikes at hard cuts.
    motion:    mean absolute luma difference to the previous sample (0..255) — how much is moving inside a shot.
    """
    err = await ff.ffmpeg(["-i", str(src), "-an", "-sn", "-vf",
                           f"fps={fps},scale=192:-2,select='gte(scene,0)',metadata=print:key=lavfi.scene_score,"
                           "format=gray,tblend=all_mode=difference,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                           "-f", "null", "-"], progress=progress, duration=duration)
    cut: dict[float, float] = {}
    mot: dict[float, float] = {}
    t = None
    for line in err.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = round(float(m.group(1)), 3)
            continue
        if t is None:
            continue
        m = re.search(r"lavfi\.scene_score=([\d.]+)", line)
        if m:
            cut[t] = float(m.group(1))
            continue
        m = re.search(r"lavfi\.signalstats\.YAVG=([\d.]+)", line)
        if m:
            mot[t] = float(m.group(1))
    return [(tt, cut.get(tt, 0.0), mot.get(tt, 0.0)) for tt in sorted(set(cut) | set(mot))]


async def loudness_profile(src: Path, step: float = 0.5, max_seconds: float = 3600) -> list[float]:
    """RMS level in dBFS per `step` seconds."""
    import numpy as np

    from ..media.beats import load_mono

    y = await load_mono(src, sr=8000, max_seconds=max_seconds)
    n = int(8000 * step)
    if len(y) < n:
        return []
    frames = y[: len(y) // n * n].reshape(-1, n)
    rms = np.sqrt((frames ** 2).mean(axis=1)) + 1e-9
    return [round(float(20 * math.log10(v)), 1) for v in rms]


def find_sidecar_subtitle(ctx: ToolContext, src: Path) -> Path | None:
    for d in (src.parent, ctx.workspace / "outputs", ctx.workspace / "subtitles"):
        for ext in (".srt", ".vtt", ".ass"):
            p = d / (src.stem + ext)
            if p.exists():
                return p
    return None


def _shots(motion: list[tuple[float, float, float]], duration: float, threshold: float, min_len: float) -> list[list[float]]:
    bounds = [0.0]
    for t, sc, _mv in motion:
        if sc >= threshold and t - bounds[-1] >= min_len and duration - t >= min_len * 0.5:
            bounds.append(t)
    bounds.append(duration)
    return [[bounds[i], bounds[i + 1]] for i in range(len(bounds) - 1) if bounds[i + 1] - bounds[i] > 0.05]


async def contact_sheets(ctx: ToolContext, src: Path, items: list[tuple[float, str]], portrait: bool,
                         cols: int, per_sheet: int, name: str) -> list[Path]:
    """Render frames at the given times (with labels) into grid images."""
    tmp = ctx.tmpdir()
    cell_w = 260 if portrait else 400
    frames: list[Path] = []
    for i, (t, label) in enumerate(items):
        f = tmp / f"f_{i:03d}.jpg"
        vf = f"scale={cell_w}:-2," + drawtext(ctx, tmp, label, size=22 if portrait else 24, x="8", y="8", box=True,
                                              box_color="black@0.65", shadow=False)
        try:
            await ff.ffmpeg(["-ss", f"{max(0.0, t):.3f}", "-i", str(src), "-frames:v", "1", "-vf", vf, "-q:v", "4", str(f)])
        except ff.FFmpegError:
            continue
        if f.exists():
            frames.append(f)
    out_dir = ctx.workspace / ".frames"
    out_dir.mkdir(exist_ok=True)
    sheets: list[Path] = []
    for k in range(0, len(frames), per_sheet):
        batch = frames[k:k + per_sheet]
        seq = tmp / f"sheet{k}"
        seq.mkdir()
        for j, f in enumerate(batch):
            f.replace(seq / f"{j:03d}.jpg")
        c = min(cols, len(batch))
        rows = math.ceil(len(batch) / c)
        sheet = out_dir / f"{name}_{int(time.time() * 1000) % 10**8}_{k // per_sheet + 1}.jpg"
        await ff.ffmpeg(["-framerate", "1", "-i", str(seq / "%03d.jpg"), "-vf",
                         f"tile={c}x{rows}:padding=4:margin=4:color=white", "-frames:v", "1", "-q:v", "3", str(sheet)])
        sheets.append(sheet)
    return sheets


def _cue_text_between(cues: list[sublib.Cue], a: float, b: float, limit: int = 120) -> str:
    def overlaps(c: sublib.Cue) -> bool:  # ignore cues that only graze the window (e.g. 0.04 s past a cut)
        ov = min(c.end, b) - max(c.start, a)
        return ov >= 0.3 or ov >= 0.4 * max(0.01, c.end - c.start)

    text = " ".join(c.text.replace("\n", " ") for c in cues if overlaps(c))
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------

@tool("video_analyze",
      "★ 看懂整段视频（剪辑前必做）：自动切分镜头，给出每个镜头的时间、运动强度(静止/平缓/中等/剧烈)、音量、台词，"
      "找出高光时刻，并返回按时间顺序排列的镜头故事板图片。比 view_frames 的均匀截图信息量大得多："
      "你会知道哪里有动作、哪里在说话、哪里是高潮。语音转写会保存为同名 .srt 供后续使用。",
      obj(["input"], input=s("视频文件"),
          transcribe=boolean("是否转写语音（默认：15 分钟以内的视频自动转写；已有同名字幕则直接复用）"),
          max_frames=integer("故事板最多多少帧，默认 36"), sensitivity=num("镜头切分灵敏度 0.15~0.5，默认 0.3（越小切得越细）")),
      category="video")
async def video_analyze(ctx: ToolContext, input: str, transcribe: bool | None = None, max_frames: int = 36,
                        sensitivity: float = 0.3) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    if not info["has_video"]:
        raise ToolError("这不是视频文件")
    dur = info["duration"] or 0.0
    portrait = (info.get("height") or 0) > (info.get("width") or 0)

    await ctx.progress(0.02, "分析画面运动与镜头")
    motion = await motion_profile(src, dur, fps=4.0 if dur <= 1200 else 1.0, progress=ctx.progress_cb(0.02, 0.35, "分析画面运动"))
    shots = _shots(motion, dur, sensitivity, min_len=0.8)

    loud: list[float] = []
    if info["has_audio"]:
        await ctx.progress(0.36, "分析声音")
        try:
            loud = await loudness_profile(src)
        except Exception:  # noqa: BLE001 — numpy missing or decode failure: analysis still useful without it
            loud = []

    cues: list[sublib.Cue] = []
    ctx_note = ""
    transcript_path = find_sidecar_subtitle(ctx, src)
    if transcript_path:
        cues = sublib.load(transcript_path)
    elif info["has_audio"] and (transcribe or (transcribe is None and dur <= 900)):
        from .subtitle import transcribe as transcribe_tool

        await ctx.progress(0.4, "转写语音")
        try:
            r = await transcribe_tool(ctx, input=str(src), output=str(ctx.out_path(src, "", ".srt")))
            transcript_path = ctx.resolve(r["subtitle"])
            cues = sublib.load(transcript_path)
        except ToolError as e:
            transcript_path = None
            ctx_note = str(e)

    # per-shot stats
    def motion_in(a: float, b: float) -> float:
        vals = [mv for t, sc, mv in motion if a + 0.3 < t < b - 0.1 and sc < sensitivity]
        return sum(vals) / len(vals) if vals else 0.0

    def loud_in(a: float, b: float) -> float | None:
        if not loud:
            return None
        seg = sorted(loud[int(a / 0.5): max(int(a / 0.5) + 1, int(b / 0.5))])
        return round(seg[max(0, int(len(seg) * 0.8) - 1)], 1) if seg else None  # ~80th percentile: speech pauses don't drag it down

    rows = []
    for i, (a, b) in enumerate(shots, 1):
        m = motion_in(a, b)
        db = loud_in(a, b)
        rows.append({"shot": i, "start": round(a, 2), "end": round(b, 2), "duration": round(b - a, 2),
                     "motion": _motion_label(m), "motion_score": round(m, 1), "volume": _loud_label(db), "db": db,
                     "speech": _cue_text_between(cues, a, b)})

    # highlights: per-second interest = motion + loudness (+ speech presence), top peaks ≥3 s apart
    highlights = []
    if dur > 3:
        secs = int(dur)
        mot = [0.0] * (secs + 1)
        for t, sc, mv in motion:
            if sc < sensitivity:  # hard cuts are not "action"
                mot[min(secs, int(t))] = max(mot[min(secs, int(t))], mv)
        lmax = max(loud) if loud else 0
        scores = []
        for sec in range(secs):
            lv = max(loud[sec * 2: sec * 2 + 2]) if loud[sec * 2: sec * 2 + 2] else -90
            l_norm = max(0.0, (lv - (lmax - 30)) / 30) if loud else 0
            score = min(1.0, mot[sec] / 20) * 0.6 + l_norm * 0.4
            scores.append((score, sec, mot[sec], lv))
        for score, sec, mv, lv in sorted(scores, reverse=True):
            if score < 0.35 or any(abs(sec - h["time"]) < 3 for h in highlights):
                continue
            why = []
            if mv >= 8:
                why.append("画面动作强")
            if loud and lv >= lmax - 6:
                why.append("声音峰值")
            line = _cue_text_between(cues, sec - 1, sec + 2, 40)
            highlights.append({"time": sec, "at": _clock(sec), "why": "+".join(why) or "综合", "speech": line})
            if len(highlights) >= 6:
                break
        highlights.sort(key=lambda h: h["time"])

    # storyboard frames: one per shot (two for long shots); if too many, keep the most informative shots
    picks: list[tuple[float, str, float]] = []
    for r in rows:
        a, b = r["start"], r["end"]
        weight = r["duration"] * 0.2 + r["motion_score"] / 10 + (1 if r["speech"] else 0)
        if b - a > 12:
            picks.append((a + (b - a) * 0.3, f"#{r['shot']}a {_clock(a + (b - a) * 0.3)}", weight))
            picks.append((a + (b - a) * 0.75, f"#{r['shot']}b {_clock(a + (b - a) * 0.75)}", weight))
        else:
            mid = a + (b - a) / 2
            picks.append((mid, f"#{r['shot']} {_clock(mid)}", weight))
    max_frames = max(6, min(48, int(max_frames)))
    if len(picks) > max_frames:
        keep = sorted(sorted(picks, key=lambda p: -p[2])[:max_frames], key=lambda p: p[0])
        picks = keep
    await ctx.progress(0.85, "生成镜头故事板")
    sheets = await contact_sheets(ctx, src, [(t, lbl) for t, lbl, _w in picks], portrait,
                                  cols=6 if portrait else 4, per_sheet=18 if portrait else 12, name=f"{src.stem}_storyboard")

    lines = [f"视频：{ctx.rel(src)} · 时长 {_clock(dur)} · {info.get('width')}x{info.get('height')} · "
             f"{info.get('fps')}fps · {'有声音' if info['has_audio'] else '无声音'}",
             f"镜头数：{len(rows)} · 平均镜头 {dur / max(1, len(rows)):.1f}s",
             "镜头 | 时间 | 时长 | 运动 | 音量 | 台词"]
    for r in rows[:160]:
        lines.append(f"#{r['shot']} | {_clock(r['start'])}–{_clock(r['end'])} | {r['duration']}s | {r['motion']} | "
                     f"{r['volume']} | {r['speech'] or '—'}")
    if len(rows) > 160:
        lines.append(f"…（共 {len(rows)} 个镜头，仅列出前 160 个）")
    result: dict[str, Any] = {
        "summary": "\n".join(lines),
        "highlights": highlights,
        "transcript": ctx.rel(transcript_path) if transcript_path else None,
        "storyboard_note": "图片中每格左上角为 #镜头号 和时间点；故事板按时间顺序排列。需要看清某段的连续动作，用 video_watch。",
        "images": [str(p) for p in sheets],
        "files": [str(p) for p in sheets],
    }
    if ctx_note:
        result["transcribe_error"] = ctx_note
    return result


def _clock_precise(t: float) -> str:
    m, s_ = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s_:05.2f}"


@tool("video_watch",
      "“观看”一段视频：连续抽帧（带时间戳）+ 这段的台词 + 音量变化。两种用法：\n"
      "① 看一段：start/end（建议 ≤20 秒），默认每秒 2~8 帧，用于看清动作的起止、找精彩瞬间。\n"
      "② 定点细看：at=某个时间点（可加 radius，默认前后各 1 秒），按接近原始帧率逐帧展开，"
      "用于排查问题——切点卡在动作中间、黑帧/闪帧、字幕和画面对不上、转场穿帮、渲染结果不对时，主动去看出问题位置附近的帧。",
      obj(["input"], input=s("视频文件"), start=time_s("开始时间（默认 0）"), end=time_s("结束时间（默认 开始+15 秒）"),
          at=time_s("定点细看的时间点（填了就忽略 start/end）"), radius=num("定点细看时前后各看多少秒，默认 1，最大 5"),
          fps=num("每秒帧数（默认自动）")), category="video")
async def video_watch(ctx: ToolContext, input: str, start: Any = 0, end: Any = None, at: Any = None,
                      radius: float = 1.0, fps: float | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    if not info["has_video"]:
        raise ToolError("这不是视频文件")
    dur = info["duration"] or 0.0
    native = float(info.get("fps") or 30) or 30.0
    focus = parse_time(at, None)
    if focus is not None:  # frame-level inspection around a moment
        r = max(0.1, min(5.0, float(radius or 1.0)))
        a, b = max(0.0, focus - r), min(dur, focus + r)
        window = b - a
        rate = fps or min(native, 36 / max(window, 0.1))
        label = _clock_precise
    else:
        a = max(0.0, parse_time(start, 0.0) or 0.0)
        b = parse_time(end, None)
        b = min(dur, b if b is not None else a + 15)
        window = b - a
        rate = fps or max(0.5, min(8.0, 24 / max(window, 0.1)))
        label = _clock
    if window < 0.05:
        raise ToolError("时间段太短")
    n = min(48, max(2, int(round(window * rate))))
    times = [a + window * (i + 0.5) / n for i in range(n)]
    portrait = (info.get("height") or 0) > (info.get("width") or 0)
    items = [(t, ("● " if focus is not None and abs(t - focus) <= window / n / 2 + 1e-6 else "") + label(t)) for t in times]
    sheets = await contact_sheets(ctx, src, items, portrait,
                                  cols=6 if portrait else 4, per_sheet=18 if portrait else 12, name=f"{src.stem}_watch")
    res: dict[str, Any] = {"window": f"{label(a)}–{label(b)}", "frames": n, "fps": round(n / window, 2),
                           "images": [str(p) for p in sheets], "files": [str(p) for p in sheets],
                           "how_to_read": "图片按时间从左到右、从上到下排列，每格左上角是该帧时间"
                                          + ("；带 ● 的是你指定的时间点" if focus is not None else "") + "。"}
    if focus is not None:
        res["frame_step"] = f"相邻两帧相隔约 {window / n:.3f} 秒（原片 {native:g} fps）"
    side = find_sidecar_subtitle(ctx, src)
    if side:
        lines = [f"{_clock(c.start)} {c.text}" for c in sublib.load(side) if c.end > a and c.start < b]
        res["speech"] = "\n".join(lines) or "（这段没有台词）"
    if info["has_audio"]:
        try:
            loud = await loudness_profile(src, step=0.5)
            seg = loud[int(a * 2): int(b * 2) + 1]
            if seg:
                bars = "▁▂▃▄▅▆▇█"
                lo, hi = min(seg), max(seg)
                res["volume_curve"] = "".join(bars[min(7, int((v - lo) / max(1e-6, hi - lo) * 7.99))] for v in seg)
                res["volume_note"] = f"每格 0.5 秒，{lo:.0f}~{hi:.0f} dB"
        except Exception:  # noqa: BLE001
            pass
    return res


def _gemini_provider(settings: dict[str, Any]) -> tuple[str, str] | None:
    for p in (settings.get("providers") or {}).values():
        if "generativelanguage.googleapis.com" in (p.get("base_url") or "") and p.get("api_key"):
            models = [m for m in p.get("models") or [] if "gemini" in m]
            return p["api_key"], (models[0] if models else "gemini-3.8-flash")
    return None


@tool("video_ask",
      "用 Gemini 原生“看”视频并回答问题（画面+声音一起理解，能描述动作、情绪、剧情、音乐，并给出时间点）。"
      "需要在设置中添加 Google Gemini 的 API Key。适合：找出所有精彩/搞笑瞬间、总结剧情、判断节奏、为解说写稿前理解内容。",
      obj(["input", "question"], input=s("视频文件"), question=s("要问的问题，例如：列出所有高能时刻及时间点"),
          start=time_s("只看这一段的开始（可选）"), end=time_s("结束（可选）")), category="video")
async def video_ask(ctx: ToolContext, input: str, question: str, start: Any = None, end: Any = None) -> dict[str, Any]:
    prov = _gemini_provider(ctx.settings)
    if not prov:
        raise ToolError("没有配置 Gemini（设置 → 模型 → 添加提供方 → Google Gemini）。可以先用 video_analyze + video_watch 理解视频。")
    key, model = prov
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    # shrink before upload: Gemini samples ~1 fps anyway, so 480p/low bitrate keeps uploads fast
    tmp = ctx.tmpdir()
    small = tmp / "upload.mp4"
    a = parse_time(start, None)
    b = parse_time(end, None)
    args: list[str] = []
    if a is not None:
        args += ["-ss", f"{a:.3f}"]
    args += ["-i", str(src)]
    if b is not None:
        args += ["-t", f"{b - (a or 0):.3f}"]
    seg_dur = (b or info["duration"]) - (a or 0)
    await ctx.progress(0.02, "压缩视频用于上传")
    await ff.ffmpeg([*args, "-vf", "scale=-2:'min(480,ih)',fps=12", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
                     "-c:a", "aac", "-b:a", "64k", "-ac", "1", str(small)], progress=ctx.progress_cb(0.02, 0.3), duration=seg_dur)
    base = "https://generativelanguage.googleapis.com"
    size = small.stat().st_size
    await ctx.progress(0.32, "上传到 Gemini")
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30)) as client:
        r = await client.post(f"{base}/upload/v1beta/files", params={"key": key}, json={"file": {"display_name": src.name}},
                              headers={"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                                       "X-Goog-Upload-Header-Content-Length": str(size),
                                       "X-Goog-Upload-Header-Content-Type": "video/mp4"})
        upload_url = r.headers.get("x-goog-upload-url")
        if r.status_code >= 400 or not upload_url:
            raise ToolError(f"Gemini 上传初始化失败 (HTTP {r.status_code})：{r.text[:300]}")
        r = await client.post(upload_url, content=small.read_bytes(),
                              headers={"X-Goog-Upload-Command": "upload, finalize", "X-Goog-Upload-Offset": "0"})
        if r.status_code >= 400:
            raise ToolError(f"Gemini 上传失败 (HTTP {r.status_code})：{r.text[:300]}")
        f = r.json().get("file") or {}
        name, uri = f.get("name"), f.get("uri")
        for _ in range(120):
            if f.get("state") == "ACTIVE":
                break
            if f.get("state") == "FAILED":
                raise ToolError("Gemini 处理视频失败")
            await asyncio.sleep(2)
            f = (await client.get(f"{base}/v1beta/{name}", params={"key": key})).json()
        await ctx.progress(0.7, "Gemini 正在观看视频")
        offset_note = f"（这段视频从原片的 {_clock(a)} 开始，回答里的时间请加上这个偏移）" if a else ""
        prompt = (f"{question}\n\n请基于你看到和听到的内容回答，涉及具体画面时给出时间点（MM:SS）。用中文回答。{offset_note}")
        r = await client.post(f"{base}/v1beta/models/{model}:generateContent", params={"key": key},
                              json={"contents": [{"parts": [{"file_data": {"mime_type": "video/mp4", "file_uri": uri}},
                                                            {"text": prompt}]}]})
        try:
            await client.delete(f"{base}/v1beta/{name}", params={"key": key})
        except httpx.HTTPError:
            pass
    if r.status_code >= 400:
        raise ToolError(f"Gemini 回答失败 (HTTP {r.status_code})：{r.text[:300]}")
    parts = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    answer = "".join(p.get("text", "") for p in parts).strip()
    return {"model": model, "answer": answer or "（Gemini 没有返回内容）"}
