"""Core video editing tools (trim, cut, concat, reframe, speed, convert, frames, silence/scene detection)."""
from __future__ import annotations

import re
import shlex
import uuid
from pathlib import Path
from typing import Any

from ..media import ffmpeg as ff
from ..media.ffmpeg import ENC_A, ENC_V, MP4_FLAGS, FFmpegError, parse_time
from .registry import ToolContext, ToolError, arr, boolean, integer, num, obj, s, time_s, tool

_FF_MAJOR: int | None = None


async def ffmpeg_major() -> int:
    global _FF_MAJOR
    if _FF_MAJOR is None:
        _c, out, _e = await ff.run_proc([ff.ffmpeg_bin(), "-version"], timeout=30)
        m = re.search(r"version\s+n?(\d+)", out)
        _FF_MAJOR = int(m.group(1)) if m else 6
    return _FF_MAJOR


async def graph_args(graph: str, tmp: Path) -> list[str]:
    """-filter_complex, spilling very long graphs to a file (Windows command-line limit)."""
    if len(graph) < 7000:
        return ["-filter_complex", graph]
    f = tmp / f"graph_{uuid.uuid4().hex[:6]}.txt"
    f.write_text(graph, "utf-8")
    if await ffmpeg_major() >= 7:
        return ["-/filter_complex", str(f)]
    return ["-filter_complex_script", str(f)]


def drawtext(ctx: ToolContext, tmp: Path, text: str, *, size: int = 48, color: str = "white",
             x: str = "(w-text_w)/2", y: str = "h*0.1", box: bool = False, box_color: str = "black@0.5",
             border: int = 0, border_color: str = "black", enable: str | None = None, alpha: str | None = None,
             bold: bool = True, font: str | None = None, shadow: bool = True) -> str:
    """drawtext filter that reads its text from a file (avoids all escaping trouble)."""
    tf = tmp / f"text_{uuid.uuid4().hex[:8]}.txt"
    tf.write_text(text, "utf-8")
    fontfile = font or ff.default_font_file(bold=bold)
    parts = [f"textfile={ff.filter_escape_path(tf)}", "expansion=none", f"fontsize={size}",
             f"fontcolor={ff.color_hex_to_ffmpeg(color)}", f"x='{x}'", f"y='{y}'", "line_spacing=8"]
    if fontfile:
        parts.insert(0, f"fontfile={ff.filter_escape_path(fontfile)}")
    if box:
        parts += ["box=1", f"boxcolor={box_color}", f"boxborderw={max(8, size // 3)}"]
    if border:
        parts += [f"borderw={border}", f"bordercolor={border_color}"]
    if shadow and not box:
        parts += ["shadowx=2", "shadowy=2", "shadowcolor=black@0.45"]
    if enable:
        parts.append(f"enable='{enable}'")
    if alpha:
        parts.append(f"alpha='{alpha}'")
    return "drawtext=" + ":".join(parts)


async def keep_segments(ctx: ToolContext, src: Path, info: dict[str, Any], segments: list[tuple[float, float]],
                        out: Path, lo: float = 0.0, hi: float = 1.0) -> float:
    """Render only the given [start, end] ranges of src, joined back to back. Returns output duration."""
    segments = [(max(0.0, a), min(info["duration"] or b, b)) for a, b in segments]
    segments = [(a, b) for a, b in segments if b - a > 0.04]
    if not segments:
        raise ToolError("没有可保留的片段")
    tmp = ctx.tmpdir()
    has_v, has_a = info.get("has_video"), info.get("has_audio")
    parts, labels = [], []
    for i, (a, b) in enumerate(segments):
        if has_v:
            parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        if has_a:
            parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels.append((f"[v{i}]" if has_v else "") + (f"[a{i}]" if has_a else ""))
    n = len(segments)
    outs = ("[v]" if has_v else "") + ("[a]" if has_a else "")
    parts.append(f"{''.join(labels)}concat=n={n}:v={1 if has_v else 0}:a={1 if has_a else 0}{outs}")
    graph = ";".join(parts)
    maps = (["-map", "[v]"] if has_v else []) + (["-map", "[a]"] if has_a else [])
    total = sum(b - a for a, b in segments)
    enc = (ENC_V if has_v else []) + (ENC_A if has_a else [])
    await ff.ffmpeg(["-i", str(src), *(await graph_args(graph, tmp)), *maps, *enc, *MP4_FLAGS, str(out)],
                    progress=ctx.progress_cb(lo, hi, "渲染中"), duration=total)
    return total


async def detect_silences(src: Path, threshold_db: float, min_silence: float) -> list[tuple[float, float]]:
    err = await ff.ffmpeg(["-i", str(src), "-vn", "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence}",
                           "-f", "null", "-"])
    out: list[tuple[float, float]] = []
    start = None
    for line in err.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            out.append((start, float(m.group(1))))
            start = None
    return out


def complement(ranges: list[tuple[float, float]], duration: float, pad: float = 0.0) -> list[tuple[float, float]]:
    keep, cur = [], 0.0
    for a, b in sorted(ranges):
        a2, b2 = a + pad, b - pad
        if b2 <= a2:
            continue
        if a2 > cur:
            keep.append((cur, a2))
        cur = max(cur, b2)
    if cur < duration:
        keep.append((cur, duration))
    return [(a, b) for a, b in keep if b - a > 0.05]


def media_result(ctx: ToolContext, out: Path, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "output": ctx.rel(out), **extra, "files": [str(out)]}


# ---------------------------------------------------------------------------

@tool("media_info", "读取视频/音频/图片的信息：时长、分辨率、帧率、编码、是否有音轨、文件大小。处理前先用它了解素材。",
      obj(["path"], path=s("文件路径（相对工作区或绝对路径）")), category="video")
async def media_info(ctx: ToolContext, path: str) -> dict[str, Any]:
    info = await ff.probe(ctx.resolve(path, must_exist=True))
    info["path"] = ctx.rel(info["path"])
    return info


@tool("video_trim", "截取视频/音频中的一段。默认精确剪切（重新编码）；fast=true 时无损快速剪切但切点会对齐到关键帧。",
      obj(["input", "start"], input=s("输入文件"), start=time_s("开始时间"), end=time_s("结束时间"),
          duration=time_s("或者指定时长"), output=s("输出路径，可省略"), fast=boolean("无损快速模式")),
      category="video")
async def video_trim(ctx: ToolContext, input: str, start: Any, end: Any = None, duration: Any = None,
                     output: str | None = None, fast: bool = False) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    a = parse_time(start, 0.0) or 0.0
    b = parse_time(end) if end is not None else (a + parse_time(duration) if duration is not None else info["duration"])
    if b is None or b <= a:
        raise ToolError("结束时间必须大于开始时间")
    out = ctx.out_path(src, "_trim", output=output)
    if fast:
        await ff.ffmpeg(["-ss", f"{a:.3f}", "-i", str(src), "-t", f"{b - a:.3f}", "-c", "copy",
                         "-avoid_negative_ts", "make_zero", str(out)])
    else:
        enc = (ENC_V if info["has_video"] else []) + (ENC_A if info["has_audio"] else [])
        await ff.ffmpeg(["-ss", f"{a:.3f}", "-i", str(src), "-t", f"{b - a:.3f}", *enc, *MP4_FLAGS, str(out)],
                        progress=ctx.progress_cb(), duration=b - a)
    return media_result(ctx, out, duration=round(b - a, 3))


@tool("video_cut", "按时间段剪辑：mode=keep 只保留这些片段并按顺序拼接；mode=remove 删掉这些片段（如口误、废片）。"
      "适合根据字幕时间轴删掉不要的内容。",
      obj(["input", "segments"], input=s("输入文件"),
          segments=arr(arr({"type": ["number", "string"]}, "[开始, 结束]"), "时间段列表，如 [[0,5.2],[10,20]]"),
          mode=s("keep 或 remove", enum=["keep", "remove"]), output=s("输出路径，可省略")),
      category="video")
async def video_cut(ctx: ToolContext, input: str, segments: list[list[Any]], mode: str = "keep",
                    output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    ranges = []
    for seg in segments:
        if not isinstance(seg, (list, tuple)) or len(seg) < 2:
            raise ToolError(f"时间段格式错误：{seg}")
        ranges.append((parse_time(seg[0]) or 0.0, parse_time(seg[1]) or 0.0))
    keep = ranges if mode == "keep" else complement(ranges, info["duration"])
    out = ctx.out_path(src, "_cut", output=output)
    total = await keep_segments(ctx, src, info, keep, out)
    return media_result(ctx, out, kept_segments=len(keep), duration=round(total, 3),
                        removed_seconds=round(info["duration"] - total, 2))


@tool("detect_silence", "检测音频中的静音/停顿片段，返回时间段。可用于了解节奏或找到可剪掉的空白。",
      obj(["input"], input=s("输入文件"), threshold_db=num("静音阈值 dB，默认 -35（越大越激进）"),
          min_silence=num("最短静音秒数，默认 0.5")), category="video")
async def detect_silence(ctx: ToolContext, input: str, threshold_db: float = -35, min_silence: float = 0.5) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    sil = await detect_silences(src, threshold_db, min_silence)
    total = sum(b - a for a, b in sil)
    return {"count": len(sil), "total_silence": round(total, 2),
            "silences": [[round(a, 2), round(b, 2)] for a, b in sil[:300]]}


@tool("remove_silence", "一键剪掉视频中的静音和停顿（口播/vlog 常用的“剪气口”），保留说话部分。",
      obj(["input"], input=s("输入文件"), threshold_db=num("静音阈值 dB，默认 -35"),
          min_silence=num("超过多少秒的静音才剪，默认 0.6"), padding=num("每段保留的缓冲秒数，默认 0.12"),
          output=s("输出路径，可省略")), category="video")
async def remove_silence(ctx: ToolContext, input: str, threshold_db: float = -35, min_silence: float = 0.6,
                         padding: float = 0.12, output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    if not info["has_audio"]:
        raise ToolError("该文件没有音轨，无法检测静音")
    await ctx.progress(0.02, "检测静音")
    sil = await detect_silences(src, threshold_db, min_silence)
    if not sil:
        return {"ok": True, "message": "没有检测到需要剪掉的静音", "output": ctx.rel(src)}
    keep = complement(sil, info["duration"], pad=padding)
    out = ctx.out_path(src, "_nosilence", output=output)
    total = await keep_segments(ctx, src, info, keep, out, lo=0.1)
    return media_result(ctx, out, original_duration=info["duration"], new_duration=round(total, 2),
                        removed_seconds=round(info["duration"] - total, 2), cuts=len(sil))


@tool("scene_detect", "检测镜头切换点（场景变化），返回每个镜头的起止时间。可用于把长素材拆成镜头再挑选精彩片段。",
      obj(["input"], input=s("输入视频"), threshold=num("灵敏度 0~1，默认 0.3（越小切点越多）"),
          min_scene=num("最短镜头秒数，默认 1.0")), category="video")
async def scene_detect(ctx: ToolContext, input: str, threshold: float = 0.3, min_scene: float = 1.0) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    err = await ff.ffmpeg(["-i", str(src), "-an", "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo",
                           "-f", "null", "-"], progress=ctx.progress_cb(), duration=info["duration"])
    cuts = [float(m.group(1)) for m in re.finditer(r"pts_time:([\d.]+)", err)]
    bounds, last = [0.0], 0.0
    for c in cuts:
        if c - last >= min_scene:
            bounds.append(c)
            last = c
    bounds.append(info["duration"])
    scenes = [[round(bounds[i], 2), round(bounds[i + 1], 2)] for i in range(len(bounds) - 1)
              if bounds[i + 1] - bounds[i] > 0.1]
    return {"count": len(scenes), "scenes": scenes[:400]}


@tool("video_concat", "把多个视频按顺序拼接成一个（自动统一分辨率/帧率，缺音轨会补静音）。需要转场效果请用 video_compose。",
      obj(["inputs"], inputs=arr(s("文件"), "按顺序的文件列表"), output=s("输出路径，可省略"),
          size=s("输出尺寸如 1920x1080 或 9:16，默认用第一个视频的尺寸"),
          fit=s("尺寸不一致时：crop 裁切 / pad 加黑边 / blur 模糊背景", enum=["crop", "pad", "blur"])),
      category="video")
async def video_concat(ctx: ToolContext, inputs: list[str], output: str | None = None, size: str | None = None,
                       fit: str = "pad") -> dict[str, Any]:
    if len(inputs) < 2:
        raise ToolError("至少需要两个文件")
    srcs = [ctx.resolve(p, must_exist=True) for p in inputs]
    infos = [await ff.probe(p) for p in srcs]
    first = infos[0]
    w, h = ff.parse_size(size, fallback=(ff.even(first.get("width") or 1920), ff.even(first.get("height") or 1080)))
    fps = round(first.get("fps") or 30) or 30
    args: list[str] = []
    parts, labels = [], []
    for i, (p, inf) in enumerate(zip(srcs, infos)):
        args += ["-i", str(p)]
    extra = len(srcs)
    for i, inf in enumerate(infos):
        if fit == "blur":
            parts.append(ff.blur_fit_graph(f"{i}:v", f"pv{i}", w, h, tag=f"b{i}"))
            parts.append(f"[pv{i}]fps={fps},format=yuv420p[v{i}]")
        else:
            parts.append(f"[{i}:v]{ff.fit_filter(w, h, fit)},fps={fps},format=yuv420p[v{i}]")
        if inf["has_audio"]:
            parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:
            args += ["-f", "lavfi", "-t", f"{inf['duration']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            parts.append(f"[{extra}:a]anull[a{i}]")
            extra += 1
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(srcs)}:v=1:a=1[v][a]")
    out = ctx.out_path(srcs[0], "_concat", ".mp4", output=output)
    total = sum(i["duration"] for i in infos)
    tmp = ctx.tmpdir()
    await ff.ffmpeg([*args, *(await graph_args(";".join(parts), tmp)), "-map", "[v]", "-map", "[a]", *ENC_V, *ENC_A,
                     *MP4_FLAGS, str(out)], progress=ctx.progress_cb(), duration=total)
    return media_result(ctx, out, duration=round(total, 2), size=f"{w}x{h}")


@tool("video_reframe", "改变画面比例，例如横屏转竖屏(9:16)做抖音/小红书/Shorts。mode: blur=模糊背景填充(推荐)，crop=居中裁切，pad=黑边。",
      obj(["input", "aspect"], input=s("输入视频"), aspect=s("目标比例：9:16 / 16:9 / 1:1 / 4:5 / 3:4，或尺寸如 1080x1920"),
          mode=s("blur / crop / pad", enum=["blur", "crop", "pad"]), focus_x=num("crop 模式下画面焦点水平位置 0~1，默认 0.5"),
          output=s("输出路径，可省略")), category="video")
async def video_reframe(ctx: ToolContext, input: str, aspect: str, mode: str = "blur", focus_x: float = 0.5,
                        output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    w, h = ff.parse_size(aspect, aspect)
    if mode == "blur":
        graph = ff.blur_fit_graph("0:v", "v", w, h)
        vargs = ["-filter_complex", graph, "-map", "[v]"] + (["-map", "0:a?"])
    elif mode == "crop":
        fx = min(1.0, max(0.0, focus_x))
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}:(iw-{w})*{fx}:(ih-{h})/2,setsar=1"
        vargs = ["-vf", vf]
    else:
        vargs = ["-vf", ff.fit_filter(w, h, "pad")]
    out = ctx.out_path(src, f"_{w}x{h}", ".mp4", output=output)
    await ff.ffmpeg(["-i", str(src), *vargs, *ENC_V, *(ENC_A if info["has_audio"] else []), *MP4_FLAGS, str(out)],
                    progress=ctx.progress_cb(), duration=info["duration"])
    return media_result(ctx, out, size=f"{w}x{h}")


@tool("video_speed", "变速：factor=2 两倍速，0.5 慢动作。音频同步变速且保持音调。",
      obj(["input", "factor"], input=s("输入文件"), factor=num("倍速 0.1~10"), output=s("输出路径，可省略"),
          smooth_slowmo=boolean("慢动作时用插帧让画面更流畅（较慢）")), category="video")
async def video_speed(ctx: ToolContext, input: str, factor: float, output: str | None = None,
                      smooth_slowmo: bool = False) -> dict[str, Any]:
    if not 0.1 <= factor <= 10:
        raise ToolError("倍速需在 0.1~10 之间")
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    vf = f"setpts=PTS/{factor}"
    if smooth_slowmo and factor < 1:
        vf += f",minterpolate=fps={round(info.get('fps') or 30)}:mi_mode=mci"
    args = ["-i", str(src)]
    if info["has_video"]:
        args += ["-vf", vf, *ENC_V]
    if info["has_audio"]:
        args += ["-af", ff.atempo_chain(factor), *ENC_A]
    out = ctx.out_path(src, f"_x{factor:g}", output=output)
    await ff.ffmpeg([*args, *MP4_FLAGS, str(out)], progress=ctx.progress_cb(), duration=info["duration"] / factor)
    return media_result(ctx, out, duration=round(info["duration"] / factor, 2))


@tool("video_convert", "转码/压缩/改格式：可设置画质 crf、最大高度(如 720)、帧率、编码(h264/h265)、目标文件大小(MB)，"
      "也可转为 mp3/wav/webm/mov 等格式。",
      obj(["input"], input=s("输入文件"), output=s("输出路径（扩展名决定格式），可省略"),
          crf=integer("画质 18(高)~32(小)，默认 23"), max_height=integer("最大高度，如 1080/720"),
          fps=num("帧率"), codec=s("h264 / h265", enum=["h264", "h265"]),
          target_size_mb=num("目标大小 MB（会自动计算码率，优先于 crf）")), category="video")
async def video_convert(ctx: ToolContext, input: str, output: str | None = None, crf: int = 23,
                        max_height: int | None = None, fps: float | None = None, codec: str = "h264",
                        target_size_mb: float | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    out = ctx.out_path(src, "_converted", ".mp4", output=output)
    ext = out.suffix.lower()
    args = ["-i", str(src)]
    if ext in ff.AUDIO_EXTS:
        args += ["-vn"] + ({"mp3": ["-c:a", "libmp3lame", "-q:a", "2"], "wav": ["-c:a", "pcm_s16le"],
                            "flac": ["-c:a", "flac"], "ogg": ["-c:a", "libvorbis"], "opus": ["-c:a", "libopus"]}
                           .get(ext[1:], ["-c:a", "aac", "-b:a", "192k"]))
    else:
        vf = []
        if max_height and (info.get("height") or 0) > max_height:
            vf.append(f"scale=-2:{max_height}")
        if fps:
            vf.append(f"fps={fps}")
        if vf:
            args += ["-vf", ",".join(vf)]
        if ext == ".webm":
            args += ["-c:v", "libvpx-vp9", "-crf", str(crf + 8), "-b:v", "0", "-c:a", "libopus"]
        else:
            vcodec = "libx265" if codec == "h265" else "libx264"
            if target_size_mb and info["duration"]:
                kbps = max(150, int(target_size_mb * 8192 / info["duration"]) - 128)
                args += ["-c:v", vcodec, "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.3)}k",
                         "-bufsize", f"{kbps * 2}k", "-preset", "medium", "-pix_fmt", "yuv420p"]
            else:
                args += ["-c:v", vcodec, "-crf", str(crf), "-preset", "medium", "-pix_fmt", "yuv420p"]
            if vcodec == "libx265" and ext in (".mp4", ".mov"):
                args += ["-tag:v", "hvc1"]
            if info["has_audio"]:
                args += ["-c:a", "aac", "-b:a", "128k"]
            if ext in (".mp4", ".mov", ".m4v"):
                args += MP4_FLAGS
    await ff.ffmpeg([*args, str(out)], progress=ctx.progress_cb(), duration=info["duration"])
    return media_result(ctx, out, size_mb=round(out.stat().st_size / 1048576, 2), original_mb=info["size_mb"])


@tool("extract_audio", "从视频中提取音频（mp3/wav/m4a）。",
      obj(["input"], input=s("输入视频"), format=s("mp3 / wav / m4a", enum=["mp3", "wav", "m4a"]),
          output=s("输出路径，可省略")), category="audio")
async def extract_audio(ctx: ToolContext, input: str, format: str = "mp3", output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    if not info["has_audio"]:
        raise ToolError("该文件没有音轨")
    out = ctx.out_path(src, "", f".{format}", output=output)
    codec = {"mp3": ["-c:a", "libmp3lame", "-q:a", "2"], "wav": ["-c:a", "pcm_s16le"],
             "m4a": ["-c:a", "aac", "-b:a", "192k"]}[format]
    await ff.ffmpeg(["-i", str(src), "-vn", *codec, str(out)], progress=ctx.progress_cb(), duration=info["duration"])
    return media_result(ctx, out)


@tool("view_frames", "“看”视频：抽取若干帧拼成一张带时间标记的缩略图发给你（需模型支持图片）。用于了解画面内容、挑选精彩镜头、检查剪辑效果。",
      obj(["input"], input=s("视频文件"), times=arr({"type": ["number", "string"]}, "指定时间点列表（可选）"),
          count=integer("均匀抽取的帧数，默认 9，最多 16"), start=time_s("均匀抽帧的起点"), end=time_s("均匀抽帧的终点")),
      category="video")
async def view_frames(ctx: ToolContext, input: str, times: list[Any] | None = None, count: int = 9,
                      start: Any = None, end: Any = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    if not info["has_video"]:
        raise ToolError("没有视频画面")
    dur = info["duration"] or 1
    if times:
        ts = [min(dur - 0.05, parse_time(t) or 0.0) for t in times][:16]
    else:
        count = max(1, min(16, int(count)))
        a = parse_time(start, 0.0) or 0.0
        b = parse_time(end, dur) or dur
        step = (b - a) / count
        ts = [a + step * (i + 0.5) for i in range(count)]
    tmp = ctx.tmpdir()
    portrait = (info.get("height") or 0) > (info.get("width") or 0)
    cell_w = 300 if portrait else 480
    for i, t in enumerate(ts):
        label = ff.fmt_time(t)[:-4] if dur >= 3600 else ff.fmt_time(t)[3:-2]
        vf = f"scale={cell_w}:-2," + drawtext(ctx, tmp, label, size=26, x="10", y="10", box=True,
                                              box_color="black@0.6", shadow=False)
        await ff.ffmpeg(["-ss", f"{max(0.0, t):.3f}", "-i", str(src), "-frames:v", "1", "-vf", vf, "-q:v", "4",
                         str(tmp / f"f_{i:02d}.jpg")])
    n = len(ts)
    cols = min(n, 4 if portrait else 3) if n > 1 else 1
    rows = (n + cols - 1) // cols
    out_dir = ctx.workspace / ".frames"
    out_dir.mkdir(exist_ok=True)
    grid = out_dir / f"{src.stem}_frames_{uuid.uuid4().hex[:6]}.jpg"
    await ff.ffmpeg(["-framerate", "1", "-i", str(tmp / "f_%02d.jpg"), "-vf",
                     f"tile={cols}x{rows}:padding=6:margin=6:color=white", "-frames:v", "1", "-q:v", "3", str(grid)])
    return {"ok": True, "frames_at": [round(t, 2) for t in ts], "grid": ctx.rel(grid), "images": [str(grid)],
            "files": [str(grid)]}


@tool("snapshot", "从视频某一时刻截取一张高清图片（可做封面素材）。",
      obj(["input"], input=s("视频文件"), time=time_s("时间点，默认第 1 秒"), output=s("输出图片路径(.jpg/.png)，可省略")),
      category="video")
async def snapshot(ctx: ToolContext, input: str, time: Any = 1, output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    t = parse_time(time, 1.0) or 0.0
    out = ctx.out_path(src, f"_{t:.1f}s", ".jpg", output=output)
    await ff.ffmpeg(["-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(out)])
    return media_result(ctx, out)


@tool("make_gif", "把视频片段做成高质量 GIF 动图（自动调色板）。",
      obj(["input"], input=s("视频文件"), start=time_s("开始时间"), duration=num("时长秒，默认 3"),
          width=integer("宽度，默认 480"), fps=integer("帧率，默认 12"), output=s("输出路径，可省略")),
      category="video")
async def make_gif(ctx: ToolContext, input: str, start: Any = 0, duration: float = 3, width: int = 480,
                   fps: int = 12, output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    out = ctx.out_path(src, "", ".gif", output=output)
    vf = (f"fps={fps},scale={width}:-1:flags=lanczos,split[s0][s1];[s0]palettegen=stats_mode=diff[p];"
          f"[s1][p]paletteuse=dither=bayer:bayer_scale=4")
    await ff.ffmpeg(["-ss", f"{parse_time(start, 0.0):.3f}", "-t", f"{duration}", "-i", str(src), "-filter_complex", vf,
                     "-loop", "0", str(out)])
    return media_result(ctx, out, size_mb=round(out.stat().st_size / 1048576, 2))


@tool("ffmpeg_run", "高级：直接运行一条 ffmpeg 命令（不含开头的 ffmpeg）。当其它工具无法满足需求时使用。工作目录为工作区，-y 已自动添加。",
      obj(["args"], args=arr(s("参数"), "参数列表，如 [\"-i\",\"in.mp4\",\"-vf\",\"hflip\",\"out.mp4\"]"),
          expected_output=s("预期生成的输出文件（用于预览），可省略")), category="video")
async def ffmpeg_run(ctx: ToolContext, args: list[str] | str, expected_output: str | None = None) -> dict[str, Any]:
    if isinstance(args, str):
        args = shlex.split(args, posix=not ff.IS_WIN)
    if args and Path(str(args[0])).stem.lower() == "ffmpeg":
        args = args[1:]
    try:
        err = await ff.ffmpeg([str(a) for a in args], cwd=ctx.workspace, timeout=3600)
    except FFmpegError as e:
        raise ToolError(str(e)) from e
    res: dict[str, Any] = {"ok": True, "log_tail": "\n".join(err.splitlines()[-8:])}
    if expected_output:
        p = ctx.resolve(expected_output)
        if p.exists():
            res["output"] = ctx.rel(p)
            res["files"] = [str(p)]
    return res
