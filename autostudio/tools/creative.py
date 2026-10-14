"""Creative tools: timeline compositor, split screen, effects, animated text, music, beats, TTS, covers."""
from __future__ import annotations

import math
import re
import shutil
from pathlib import Path
from typing import Any

from ..media import ffmpeg as ff
from ..media import subs as sublib
from ..media.tts import EDGE_PERSONAS
from ..media.ffmpeg import ENC_A, MP4_FLAGS, parse_time
from .registry import ToolContext, ToolError, any_of_obj, arr, boolean, integer, num, obj, s, time_s, tool
from .video import drawtext, graph_args, media_result

TRANSITIONS = [
    "fade", "fadeblack", "fadewhite", "dissolve", "wipeleft", "wiperight", "wipeup", "wipedown", "slideleft",
    "slideright", "slideup", "slidedown", "smoothleft", "smoothright", "smoothup", "smoothdown", "circleopen",
    "circleclose", "circlecrop", "rectcrop", "vertopen", "vertclose", "horzopen", "horzclose", "radial", "pixelize",
    "zoomin", "diagtl", "diagtr", "diagbl", "diagbr", "hlslice", "hrslice", "vuslice", "vdslice", "hblur",
    "fadegrays", "squeezeh", "squeezev", "distance",
]

EFFECT_NAMES = [
    "cinematic", "teal_orange", "vintage", "bw", "sepia", "vivid", "warm", "cool", "cyberpunk", "dreamy_glow",
    "film_grain", "vignette", "blur", "sharpen", "denoise", "mirror", "flip", "rotate_90", "rotate_180",
    "zoom_in", "zoom_out", "shake", "glitch", "rgb_split", "pixelate", "flash", "fade_in", "fade_out",
    "letterbox", "reverse", "boomerang", "stabilize", "night_vision", "lut",
]


# ---------------------------------------------------------------------------
# helpers

def _pos_xy(position: str | None, w: int, h: int, x: Any = None, y: Any = None) -> tuple[str, str]:
    def val(v: Any, total: int, axis: str) -> str:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)   # an ffmpeg expression, e.g. "w*0.1+200*t" or "if(lt(t,1),-text_w,80)"
        return str(int(f * total)) if 0 <= f <= 1 else str(int(f))

    if x is not None and y is not None:
        return val(x, w, "x"), val(y, h, "y")
    pos = (position or "center").lower().replace("-", "_")
    xs = {"left": "w*0.06", "right": "w*0.94-text_w", "center": "(w-text_w)/2"}
    ys = {"top": "h*0.08", "upper": "h*0.22", "center": "(h-text_h)/2", "middle": "(h-text_h)/2",
          "lower": "h*0.68-text_h/2", "bottom": "h*0.86-text_h"}
    parts = pos.split("_")
    xk = next((p for p in parts if p in xs), "center")
    yk = next((p for p in parts if p in ys), "center")
    if pos in ("left", "right"):
        yk = "center"
    return xs[xk], ys[yk]


def text_filters(ctx: ToolContext, tmp: Path, items: list[dict[str, Any]], w: int, h: int,
                 default_end: float) -> list[str]:
    """Animated drawtext filters for a list of text items (times relative to the stream)."""
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            it = {"text": it}
        text = str(it.get("text") or "").replace("\\n", "\n")
        if not text.strip():
            continue
        a = parse_time(it.get("start"), 0.0) or 0.0
        b = parse_time(it.get("end"), None)
        if b is None:
            dur = parse_time(it.get("duration"), None)
            b = a + dur if dur else default_end
        size = int(it.get("size") or round(min(w, h) * (0.075 if it.get("style") == "title" else 0.05)))
        x, y = _pos_xy(it.get("position"), w, h, it.get("x"), it.get("y"))
        anim = (it.get("animation") or "fade").lower()
        fd = min(0.35, (b - a) / 3)
        enable = f"between(t,{a:.3f},{b:.3f})"
        alpha = None
        if anim in ("fade", "slide_up", "slide_down", "slide_left", "pop"):
            alpha = f"if(lt(t,{a + fd:.3f}),(t-{a:.3f})/{fd:.3f},if(gt(t,{b - fd:.3f}),({b:.3f}-t)/{fd:.3f},1))"
        if it.get("alpha") is not None:   # the author's own opacity expression replaces the preset animation
            alpha = str(it["alpha"])
        if anim == "slide_up":
            y = f"({y})+{int(h * 0.04)}*max(0,1-(t-{a:.3f})/{fd * 1.4:.3f})"
        elif anim == "slide_down":
            y = f"({y})-{int(h * 0.04)}*max(0,1-(t-{a:.3f})/{fd * 1.4:.3f})"
        elif anim == "slide_left":
            x = f"({x})+{int(w * 0.08)}*max(0,1-(t-{a:.3f})/{fd * 1.4:.3f})"
        common = dict(size=size, color=it.get("color") or "white", box=bool(it.get("box")),
                      font=ff.font_file(it["font"], bool(it.get("bold", True))) if it.get("font") else None,
                      box_color=ff.color_hex_to_ffmpeg(it.get("box_color"), "black") + "@0.55",
                      border=int(it.get("border") if it.get("border") is not None else max(2, size // 16)),
                      border_color=it.get("border_color") or "black", bold=it.get("bold", True))
        if anim == "typewriter":
            chars = list(text)[:60]
            step = min(0.08, (b - a) * 0.5 / max(1, len(chars)))
            for i in range(1, len(chars) + 1):
                t0 = a + step * (i - 1)
                t1 = a + step * i if i < len(chars) else b
                out.append(drawtext(ctx, tmp, "".join(chars[:i]), x=x, y=y, enable=f"between(t,{t0:.3f},{t1:.3f})",
                                    **common))
            continue
        out.append(drawtext(ctx, tmp, text, x=x, y=y, enable=enable, alpha=alpha, **common))
    return out


def effect_filter(name: str, w: int, h: int, fps: float, dur: float, params: dict[str, Any], uid: str) -> str:
    """Single-input/single-output filter (may contain internal labels)."""
    st = float(params.get("strength") or 1.0)
    n = name.lower()
    if n == "cinematic":
        bar = max(0, int((h - w / 2.39) / 2)) if w > h else int(h * 0.08)
        return (f"eq=contrast=1.08:saturation=1.08:gamma=0.97,colorbalance=rs=-0.04:bs=0.07:rh=0.07:bh=-0.05,"
                f"drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill,drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill")
    if n == "teal_orange":
        return f"colorbalance=rs={-0.1 * st:.3f}:bs={0.15 * st:.3f}:rh={0.12 * st:.3f}:bh={-0.12 * st:.3f},eq=saturation=1.15"
    if n == "vintage":
        return "curves=preset=vintage,noise=alls=10:allf=t,vignette=PI/4"
    if n == "bw":
        return "hue=s=0,eq=contrast=1.18"
    if n == "sepia":
        return "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"
    if n == "vivid":
        return f"eq=saturation={1 + 0.35 * st:.2f}:contrast=1.06"
    if n == "warm":
        return f"colorbalance=rs={0.08 * st:.3f}:gs={0.02 * st:.3f}:bs={-0.08 * st:.3f}:rm={0.05 * st:.3f}:bm={-0.05 * st:.3f}"
    if n == "cool":
        return f"colorbalance=rs={-0.08 * st:.3f}:bs={0.1 * st:.3f}:rm={-0.04 * st:.3f}:bm={0.06 * st:.3f}"
    if n == "cyberpunk":
        return "colorbalance=rs=0.18:gs=-0.08:bs=0.25:rh=0.1:bh=0.15,eq=saturation=1.45:contrast=1.12"
    if n == "dreamy_glow":
        return (f"split[{uid}a][{uid}b];[{uid}b]gblur=sigma={18 * st:.1f}[{uid}g];"
                f"[{uid}a][{uid}g]blend=all_mode=screen:all_opacity={0.35 * st:.2f}")
    if n == "film_grain":
        return f"noise=alls={int(16 * st)}:allf=t+u"
    if n == "vignette":
        return "vignette=PI/4"
    if n == "blur":
        return f"gblur=sigma={12 * st:.1f}"
    if n == "sharpen":
        return "unsharp=5:5:1.0"
    if n == "denoise":
        return "hqdn3d=3:3:6:6"
    if n == "mirror":
        return "hflip"
    if n == "flip":
        return "vflip"
    if n == "rotate_90":
        return "transpose=1"
    if n == "rotate_180":
        return "transpose=1,transpose=1"
    if n in ("zoom_in", "zoom_out"):
        z = 0.18 * st
        frames = max(1, int(dur * fps))
        expr = f"1+{z:.3f}*on/{frames}" if n == "zoom_in" else f"{1 + z:.3f}-{z:.3f}*on/{frames}"
        return (f"scale={w * 2}:{h * 2},zoompan=z='{expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:"
                f"fps={fps:g},setsar=1")
    if n == "shake":
        a = 0.6 * st
        return (f"crop=iw*0.92:ih*0.92:(iw-ow)/2+(iw-ow)/2*{a:.2f}*sin(t*17)*cos(t*7):"
                f"(ih-oh)/2+(ih-oh)/2*{a:.2f}*cos(t*13)*sin(t*5),scale={w}:{h},setsar=1")
    if n == "glitch":
        return ("rgbashift=rh=-12:bh=12:rv=4:enable='lt(mod(t,1.9),0.14)',"
                "noise=alls=40:allf=t:enable='lt(mod(t,1.9),0.1)',"
                "hue=h=60:enable='lt(mod(t+0.05,3.1),0.06)'")
    if n == "rgb_split":
        k = int(6 * st)
        return f"rgbashift=rh=-{k}:bh={k}"
    if n == "pixelate":
        k = max(2, int(16 * st))
        return f"scale=iw/{k}:ih/{k}:flags=neighbor,scale={w}:{h}:flags=neighbor"
    if n == "flash":
        return "fade=t=in:st=0:d=0.3:color=white"
    if n == "fade_in":
        return f"fade=t=in:st=0:d={float(params.get('duration') or 0.6):.2f}"
    if n == "fade_out":
        d = float(params.get("duration") or 0.8)
        return f"fade=t=out:st={max(0.0, dur - d):.2f}:d={d:.2f}"
    if n == "letterbox":
        bar = int(h * 0.1)
        return f"drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill,drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill"
    if n == "reverse":
        return "reverse"
    if n == "boomerang":
        return f"split[{uid}f][{uid}r];[{uid}r]reverse[{uid}rv];[{uid}f][{uid}rv]concat=n=2:v=1:a=0"
    if n == "stabilize":
        return "deshake"
    if n == "night_vision":
        return "colorchannelmixer=.2:.6:.2:0:.3:.9:.3:0:.1:.3:.1,noise=alls=25:allf=t,vignette=PI/3"
    if n == "lut":
        f = params.get("file")
        if not f:
            raise ToolError("lut 效果需要 file 参数（.cube 文件）")
        return f"lut3d=file={ff.filter_escape_path(f)}"
    raise ToolError(f"未知效果：{name}。可用：{', '.join(EFFECT_NAMES)}")


def effects_chain(effects: list[Any] | None, w: int, h: int, fps: float, dur: float, prefix: str,
                  ctx: ToolContext | None = None) -> str:
    parts = []
    for i, e in enumerate(effects or []):
        if isinstance(e, str):
            name, params = e, {}
        else:
            name, params = str(e.get("name") or e.get("type") or ""), dict(e)
        if name.lower() == "lut" and ctx and params.get("file"):
            params["file"] = str(ctx.resolve(params["file"], must_exist=True))
        parts.append(effect_filter(name, w, h, fps, dur, params, f"{prefix}e{i}"))
    return ",".join(parts)


def _auto_fit(info: dict[str, Any], w: int, h: int) -> str:
    sw, sh = info.get("width") or w, info.get("height") or h
    ratio = (sw / sh) / (w / h)
    return "crop" if 0.85 <= ratio <= 1.18 else "blur"


def _fit_chain(inp: str, out: str, w: int, h: int, mode: str, tag: str, bg: str | None = None) -> str:
    if mode == "blur":
        return ff.blur_fit_graph(inp, out, w, h, tag=tag)
    return f"[{inp}]{ff.fit_filter(w, h, mode, ff.color_hex_to_ffmpeg(bg, 'black'))}[{out}]"


async def join_clips(ctx: ToolContext, rendered: list[tuple[Path, float]], trans: list[tuple[str, float]], tmp: Path,
                     fps: int, crf: int, prog: Any = None) -> tuple[Path, float]:
    """Join normalized clips; trans[k] is the transition from clip k to k+1 ("none" = cut). → (file, length)."""
    joined = tmp / "joined.mp4"
    if all(n == "none" for n, _d in trans) or len(rendered) == 1:
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p, _d in rendered), "utf-8")
        await ff.ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])
        return joined, sum(d for _p, d in rendered)
    args: list[str] = []
    for p, _d in rendered:
        args += ["-i", str(p)]
    parts = [f"[{i}:v]fps={fps},settb=AVTB,format=yuv420p[v{i}];[{i}:a]asettb=AVTB[a{i}]" for i in range(len(rendered))]
    vcur, acur = "v0", "a0"
    length = rendered[0][1]
    for k in range(1, len(rendered)):
        name, d = trans[k - 1]
        dk = rendered[k][1]
        if name == "none" or d <= 0:
            parts.append(f"[{vcur}][v{k}]concat=n=2:v=1:a=0[vx{k}]")
            parts.append(f"[{acur}][a{k}]concat=n=2:v=0:a=1[ax{k}]")
            length += dk
        else:
            d = max(0.1, min(d, length - 0.05, dk - 0.05))
            off = length - d
            parts.append(f"[{vcur}][v{k}]xfade=transition={name}:duration={d:.3f}:offset={off:.3f}[vx{k}]")
            parts.append(f"[{acur}][a{k}]acrossfade=d={d:.3f}:c1=tri:c2=tri[ax{k}]")
            length = length + dk - d
        vcur, acur = f"vx{k}", f"ax{k}"
    await ff.ffmpeg([*args, *(await graph_args(";".join(parts), tmp)), "-map", f"[{vcur}]", "-map", f"[{acur}]",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
                     *ENC_A, str(joined)], progress=prog, duration=length)
    return joined, length


# ---------------------------------------------------------------------------
# video_compose: the timeline renderer

async def _render_clip(ctx: ToolContext, clip: dict[str, Any], i: int, w: int, h: int, fps: int, tmp: Path,
                       default_fit: str | None, crf: int, prog: Any) -> tuple[Path, float]:
    out = tmp / f"clip_{i:03d}.mp4"
    speed = float(clip.get("speed") or 1.0)
    volume = float(clip.get("volume") if clip.get("volume") is not None else 1.0)
    args: list[str] = []
    graph: list[str] = []
    src_info: dict[str, Any] = {}
    kind = "color"
    if clip.get("src"):
        src = ctx.resolve(clip["src"], must_exist=True)
        kind = "image" if src.suffix.lower() in ff.IMAGE_EXTS else "video"
        if kind == "video":
            src_info = await ff.probe(src)
            if not src_info["has_video"]:
                raise ToolError(f"片段 {i + 1} 不是视频：{clip['src']}")
            a = parse_time(clip.get("start"), 0.0) or 0.0
            b = parse_time(clip.get("end"), None)
            if b is None:
                d = parse_time(clip.get("duration"), None)
                b = a + d * speed if d else src_info["duration"]
            b = min(b, src_info["duration"])
            if b - a < 0.1:
                raise ToolError(f"片段 {i + 1} 时长过短（{a}~{b}）")
            dur = (b - a) / speed
            args += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(src)]
        else:
            dur = parse_time(clip.get("duration"), 3.0) or 3.0
            args += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", str(src)]
    else:
        dur = parse_time(clip.get("duration"), 2.5) or 2.5
        color = ff.color_hex_to_ffmpeg(clip.get("color"), "black")
        args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"color=c={color}:s={w}x{h}:r={fps}"]

    # ---- video chain
    v = "0:v"
    if kind == "video":
        pre = []
        if speed != 1.0:
            pre.append(f"setpts=PTS/{speed}")
        if pre:
            graph.append(f"[{v}]{','.join(pre)}[sp]")
            v = "sp"
        mode = clip.get("fit") or default_fit or _auto_fit(src_info, w, h)
        graph.append(_fit_chain(v, "fit", w, h, mode, f"c{i}", clip.get("bg")))
        v = "fit"
    elif kind == "image":
        kb = (clip.get("kenburns") or "zoom_in").lower()
        frames = max(1, int(dur * fps))
        z = 0.15
        zexpr = {"zoom_in": f"1+{z}*on/{frames}", "zoom_out": f"{1 + z}-{z}*on/{frames}"}.get(kb, f"{1 + z / 2}")
        xexpr, yexpr = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
        if kb == "pan_left":
            xexpr = f"(iw-iw/zoom)*(1-on/{frames})"
        elif kb == "pan_right":
            xexpr = f"(iw-iw/zoom)*on/{frames}"
        elif kb == "pan_up":
            yexpr = f"(ih-ih/zoom)*(1-on/{frames})"
        elif kb == "pan_down":
            yexpr = f"(ih-ih/zoom)*on/{frames}"
        if kb == "none":
            mode = clip.get("fit") or "blur"
            graph.append(_fit_chain("0:v", "fit", w, h, mode, f"c{i}", clip.get("bg")))
        else:
            graph.append(f"[0:v]scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
                         f"zoompan=z='{zexpr}':x='{xexpr}':y='{yexpr}':d=1:s={w}x{h}:fps={fps},setsar=1[fit]")
        v = "fit"
    else:
        graph.append("[0:v]setsar=1[fit]")
        v = "fit"

    post = [f"fps={fps}"]
    fx = effects_chain(clip.get("effects"), w, h, fps, dur, f"c{i}", ctx)
    if fx:
        post.append(fx)
    if clip.get("vf"):   # raw ffmpeg filter chain written by the author
        post.append(str(clip["vf"]))
    texts = clip.get("text") or clip.get("texts")
    if texts:
        items = texts if isinstance(texts, list) else [texts]
        items = [({"text": t} if isinstance(t, str) else t) for t in items]
        if not clip.get("src"):
            for t in items:
                t.setdefault("style", "title")
        post += text_filters(ctx, tmp, items, w, h, dur)
    post.append("format=yuv420p")
    graph.append(f"[{v}]{','.join(post)}[vout]")

    # ---- audio chain
    if kind == "video" and src_info.get("has_audio") and volume > 0:
        achain = []
        if speed != 1.0:
            achain.append(ff.atempo_chain(speed))
        if volume != 1.0:
            achain.append(f"volume={volume}")
        achain += ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        graph.append(f"[0:a]{','.join(achain)}[aout]")
    else:
        args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        graph.append("[1:a]aformat=sample_fmts=fltp:channel_layouts=stereo[aout]")

    await ff.ffmpeg([*args, *(await graph_args(";".join(graph), tmp)), "-map", "[vout]", "-map", "[aout]",
                     "-t", f"{dur:.3f}", "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                     "-pix_fmt", "yuv420p", *ENC_A, str(out)], progress=prog, duration=dur)
    real = (await ff.probe(out))["duration"] or dur
    return out, real


def _snap_to_beats(durations: list[float], transitions: list[float], beats: list[float], offset: float,
                   max_lens: list[float]) -> list[float]:
    """Adjust clip durations so every cut lands on the nearest beat."""
    beats = [b - offset for b in beats if b - offset > 0.3]
    if not beats:
        return durations
    out = list(durations)
    t = 0.0
    for i in range(len(out) - 1):
        target = t + out[i] - (transitions[i] if i < len(transitions) else 0) / 2
        nearest = min(beats, key=lambda b: abs(b - target))
        new_end = nearest + (transitions[i] if i < len(transitions) else 0) / 2
        new_len = max(0.4, min(max_lens[i], new_end - t))
        out[i] = new_len
        t += new_len - (transitions[i] if i < len(transitions) else 0)
    return out


@tool("video_compose",
      "★ 时间线合成引擎：一次性渲染一个完整短视频。可组合视频片段/图片/纯色标题卡，设置每段的入出点、变速、滤镜特效、"
      "文字动画，片段之间加转场，再叠加背景音乐(可自动踩点 beat_sync、人声闪避 duck)、配音、全局文字、字幕、片头片尾淡入淡出。"
      f"转场可选：{', '.join(TRANSITIONS)}，或 none。特效可选：{', '.join(EFFECT_NAMES)}。"
      "图片默认带 Ken Burns 缓慢推镜(kenburns: zoom_in/zoom_out/pan_left/pan_right/pan_up/pan_down/none)。"
      "文字动画 animation: fade/slide_up/slide_down/slide_left/typewriter/none；position: top/upper/center/lower/bottom，可加 _left/_right。",
      obj(["clips"],
          clips=arr(any_of_obj(
              "片段对象：{src: 视频或图片路径(纯色卡则省略), color: '#111111'(纯色卡), start/end: 视频入出点, "
              "duration: 时长(图片/纯色卡必填，视频可替代 end), speed: 倍速, volume: 原声音量(0 静音), "
              "fit: crop/pad/blur, effects: [特效名或 {name, strength}], kenburns: 图片运镜, "
              "text: 文字或 {text, position, size, color, animation, start, end, box}, "
              "transition: 到下一段的转场名或 {type, duration}}"), "片段列表，按时间顺序"),
          output=s("输出路径，可省略"),
          size=s("输出尺寸，如 1080x1920 / 1920x1080，或比例 9:16、16:9、1:1、4:5"),
          fps=integer("帧率，默认 30"),
          fit=s("默认画面适配：crop / pad / blur，默认自动"),
          transition=any_of_obj("默认转场，如 {type:'fade', duration:0.5}，或字符串 'fade'；none 为硬切"),
          music=any_of_obj("背景音乐 {src, volume:0.3, start:音乐起始秒, fade_in:1, fade_out:2, duck:true 有人声时自动压低, "
                           "beat_sync:true 让剪辑点对齐节拍}"),
          voiceover=any_of_obj("配音 {src, volume:1.0, start:0}"),
          texts=arr(any_of_obj("{text, start, end, position, size, color, animation, box, style:'title'}"),
                    "全局文字（时间为成片时间轴）"),
          subtitles=any_of_obj("烧录字幕 {src: srt路径(成片时间轴), font_size, color, position, box, animation:'pop'/'fade'}"),
          fade_in=num("片头淡入秒数"), fade_out=num("片尾淡出秒数"),
          quality=s("high / medium / small", enum=["high", "medium", "small"])),
      category="creative")
async def video_compose(ctx: ToolContext, clips: list[dict[str, Any]], output: str | None = None,
                        size: str | None = None, fps: int = 30, fit: str | None = None, transition: Any = None,
                        music: dict[str, Any] | None = None, voiceover: dict[str, Any] | None = None,
                        texts: list[dict[str, Any]] | None = None, subtitles: dict[str, Any] | str | None = None,
                        fade_in: float = 0, fade_out: float = 0, quality: str = "high") -> dict[str, Any]:
    if not clips:
        raise ToolError("clips 不能为空")
    clips = [({"src": c} if isinstance(c, str) else dict(c)) for c in clips]
    # output size: explicit, else from first visual source
    first_info = None
    for c in clips:
        if c.get("src"):
            p = ctx.resolve(c["src"], must_exist=True)
            if p.suffix.lower() not in ff.IMAGE_EXTS:
                first_info = await ff.probe(p)
                break
    fallback = (ff.even(first_info["width"]), ff.even(first_info["height"])) if first_info and first_info.get("width") else (1080, 1920)
    w, h = ff.parse_size(size, size, fallback=fallback)
    if max(w, h) > 3840:
        raise ToolError("输出尺寸过大")
    crf = {"high": 18, "medium": 21, "small": 25}.get(quality, 18)
    tmp = ctx.tmpdir()

    def tr_of(c: dict[str, Any]) -> tuple[str, float]:
        t = c.get("transition", transition)
        if t is None or t == "" or t == "none" or t == "cut":
            return "none", 0.0
        if isinstance(t, str):
            return t, 0.5
        return str(t.get("type") or "fade"), float(t.get("duration") or 0.5)

    trans = [tr_of(c) for c in clips[:-1]]
    for name, _d in trans:
        if name != "none" and name not in TRANSITIONS:
            raise ToolError(f"未知转场：{name}。可用：{', '.join(TRANSITIONS)}")

    # beat sync: pre-compute desired clip durations
    music_info = None
    if music and music.get("src"):
        music_path = ctx.resolve(music["src"], must_exist=True)
        music_info = await ff.probe(music_path)
        if music.get("beat_sync"):
            from ..media.beats import analyze, load_mono

            await ctx.progress(0.01, "分析音乐节拍")
            y = await load_mono(music_path, max_seconds=600)
            beats = analyze(y)["beats"]
            durs, maxes = [], []
            for c in clips:
                if c.get("src") and ctx.resolve(c["src"]).suffix.lower() not in ff.IMAGE_EXTS:
                    inf = await ff.probe(ctx.resolve(c["src"]))
                    a = parse_time(c.get("start"), 0.0) or 0.0
                    b = parse_time(c.get("end"), None)
                    sp = float(c.get("speed") or 1)
                    if b is None:
                        d = parse_time(c.get("duration"), None)
                        b = a + d * sp if d else inf["duration"]
                    durs.append((b - a) / sp)
                    maxes.append((inf["duration"] - a) / sp)
                else:
                    d = parse_time(c.get("duration"), 3.0) or 3.0
                    durs.append(d)
                    maxes.append(1e9)
            snapped = _snap_to_beats(durs, [d for _n, d in trans], beats,
                                     parse_time(music.get("start"), 0.0) or 0.0, maxes)
            for c, d in zip(clips, snapped):
                if c.get("src") and ctx.resolve(c["src"]).suffix.lower() not in ff.IMAGE_EXTS:
                    a = parse_time(c.get("start"), 0.0) or 0.0
                    c["end"] = a + d * float(c.get("speed") or 1)
                    c.pop("duration", None)
                else:
                    c["duration"] = d

    # 1) render every clip to a normalized intermediate
    rendered: list[tuple[Path, float]] = []
    est = [max(0.5, float(parse_time(c.get("duration"), 3.0) or 3.0)) for c in clips]
    total_est = sum(est)
    done = 0.0
    for i, c in enumerate(clips):
        lo = 0.03 + 0.6 * done / total_est
        hi = 0.03 + 0.6 * (done + est[i]) / total_est
        await ctx.progress(lo, f"渲染片段 {i + 1}/{len(clips)}")
        rendered.append(await _render_clip(ctx, c, i, w, h, fps, tmp, fit, crf, ctx.progress_cb(lo, hi,
                                                                                               f"渲染片段 {i + 1}/{len(clips)}")))
        done += est[i]

    # 2) join with transitions
    await ctx.progress(0.64, "拼接与转场")
    joined, total = await join_clips(ctx, rendered, trans, tmp, fps, crf, ctx.progress_cb(0.64, 0.8, "拼接与转场"))

    # 3) final pass: global texts / subtitles / music / voiceover / fades
    out = ctx.out_path(None, "", ".mp4", output=output) if output else ctx.out_path(Path("compose.mp4"), "", ".mp4")
    sub_src = subtitles.get("src") if isinstance(subtitles, dict) else subtitles
    need_final = bool(texts or sub_src or (music and music.get("src")) or (voiceover and voiceover.get("src"))
                      or fade_in or fade_out)
    if not need_final:
        shutil.move(str(joined), out)
        return media_result(ctx, out, duration=round(total, 2), size=f"{w}x{h}", clips=len(clips))

    args = ["-i", str(joined)]
    vparts: list[str] = []
    if texts:
        vparts += text_filters(ctx, tmp, texts, w, h, total)
    if sub_src:
        style = dict(subtitles) if isinstance(subtitles, dict) else {}
        cues = sublib.load(ctx.resolve(sub_src, must_exist=True))
        fd = ff.fonts_dir_for(style.get("font"))
        style["font"] = ff.font_name(style.get("font"))
        ass = tmp / "subs.ass"
        sublib.save(ass, cues, width=w, height=h, style=style)
        vparts.append(f"ass={ff.filter_escape_path(ass)}" + (f":fontsdir={ff.filter_escape_path(fd)}" if fd else ""))
    if fade_in:
        vparts.append(f"fade=t=in:st=0:d={fade_in}")
    if fade_out:
        vparts.append(f"fade=t=out:st={max(0, total - fade_out):.3f}:d={fade_out}")
    graph = [f"[0:v]{','.join(vparts) if vparts else 'null'}[vout]"]

    amix_inputs = ["[base]"]
    graph.append("[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[base]")
    idx = 1
    if voiceover and voiceover.get("src"):
        vo = ctx.resolve(voiceover["src"], must_exist=True)
        args += ["-i", str(vo)]
        delay = int((parse_time(voiceover.get("start"), 0.0) or 0) * 1000)
        graph.append(f"[{idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                     f"volume={float(voiceover.get('volume') or 1.0)},adelay={delay}|{delay}[vo]")
        amix_inputs.append("[vo]")
        idx += 1
    if music and music.get("src"):
        mp = ctx.resolve(music["src"], must_exist=True)
        mstart = parse_time(music.get("start"), 0.0) or 0.0
        args += ["-stream_loop", "-1", "-ss", f"{mstart:.3f}", "-i", str(mp)]
        vol = float(music.get("volume") if music.get("volume") is not None else 0.3)
        mfi = float(music.get("fade_in") if music.get("fade_in") is not None else 1.0)
        mfo = float(music.get("fade_out") if music.get("fade_out") is not None else 2.0)
        chain = (f"[{idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,atrim=0:{total:.3f},"
                 f"asetpts=PTS-STARTPTS,volume={vol}")
        if mfi:
            chain += f",afade=t=in:st=0:d={mfi}"
        if mfo:
            chain += f",afade=t=out:st={max(0.0, total - mfo):.3f}:d={mfo}"
        graph.append(chain + "[bgm]")
        idx += 1
        if music.get("duck", True):
            speech = "[base]" if len(amix_inputs) == 1 else None
            if speech is None:
                graph.append(f"{''.join(amix_inputs)}amix=inputs={len(amix_inputs)}:duration=first:normalize=0[speech]")
                amix_inputs = ["[speech]"]
            graph.append(f"{amix_inputs[0]}asplit=2[sp1][sp2]")
            graph.append("[bgm][sp2]sidechaincompress=threshold=0.03:ratio=8:attack=30:release=400[bgmd]")
            amix_inputs = ["[sp1]", "[bgmd]"]
        else:
            amix_inputs.append("[bgm]")
    if len(amix_inputs) > 1:
        graph.append(f"{''.join(amix_inputs)}amix=inputs={len(amix_inputs)}:duration=first:normalize=0[amixed]")
        alabel = "amixed"
    else:
        alabel = amix_inputs[0].strip("[]")
    afx = []
    if fade_in:
        afx.append(f"afade=t=in:st=0:d={fade_in}")
    if fade_out:
        afx.append(f"afade=t=out:st={max(0, total - fade_out):.3f}:d={fade_out}")
    afx.append("alimiter=limit=0.95")
    graph.append(f"[{alabel}]{','.join(afx)}[aout]")
    await ctx.progress(0.82, "叠加音乐 / 文字 / 字幕")
    await ff.ffmpeg([*args, *(await graph_args(";".join(graph), tmp)), "-map", "[vout]", "-map", "[aout]",
                     "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                     "-pix_fmt", "yuv420p", *ENC_A, *MP4_FLAGS, str(out)],
                    progress=ctx.progress_cb(0.82, 1.0, "最终合成"), duration=total)
    return media_result(ctx, out, duration=round(total, 2), size=f"{w}x{h}", clips=len(clips),
                        music=bool(music_info))


# ---------------------------------------------------------------------------

@tool("video_layout",
      "分屏 / 多宫格 / 画中画：把 2~6 个视频同时放在一个画面里。layout: side_by_side 左右分屏，top_bottom 上下分屏，"
      "grid 宫格(自动 2x2/3x2)，pip 画中画(第二个视频悬浮在第一个上)，reaction 反应视频(上大下小，适合竖屏)。",
      obj(["inputs"], inputs=arr(s("视频"), "视频列表"),
          layout=s("布局", enum=["side_by_side", "top_bottom", "grid", "pip", "reaction"]),
          size=s("输出尺寸，如 1920x1080 / 1080x1920，默认按布局自动"),
          audio=s("声音来源：first 只用第一个 / mix 混合全部 / 数字索引"),
          duration=s("shortest 以最短为准 / longest 以最长为准 / first 以第一个为准"),
          gap=integer("画面间隔像素，默认 0"), background=s("背景色，默认 black"),
          pip_position=s("画中画位置：top_left/top_right/bottom_left/bottom_right"),
          pip_scale=num("画中画大小（占宽度比例），默认 0.3"), border=integer("画中画边框像素，默认 6"),
          labels=arr(s("标签"), "每个画面左上角的文字标签（可选），如 ['Before','After']"),
          output=s("输出路径，可省略")), category="creative")
async def video_layout(ctx: ToolContext, inputs: list[str], layout: str = "side_by_side", size: str | None = None,
                       audio: str = "first", duration: str = "shortest", gap: int = 0, background: str = "black",
                       pip_position: str = "bottom_right", pip_scale: float = 0.3, border: int = 6,
                       labels: list[str] | None = None, output: str | None = None) -> dict[str, Any]:
    if len(inputs) < 2:
        raise ToolError("至少需要两个视频")
    if len(inputs) > 6:
        raise ToolError("最多 6 个视频")
    srcs = [ctx.resolve(p, must_exist=True) for p in inputs]
    infos = [await ff.probe(p) for p in srcs]
    n = len(srcs)
    defaults = {"side_by_side": (1920, 1080), "top_bottom": (1080, 1920), "grid": (1920, 1080),
                "reaction": (1080, 1920)}
    if layout == "pip":
        fb = (ff.even(infos[0].get("width") or 1920), ff.even(infos[0].get("height") or 1080))
    else:
        fb = defaults.get(layout, (1920, 1080))
    W, H = ff.parse_size(size, size, fallback=fb)
    g = int(gap)
    cells: list[tuple[int, int, int, int]] = []
    if layout == "side_by_side":
        cw = ff.even((W - g * (n - 1)) / n)
        cells = [(i * (cw + g), 0, cw, H) for i in range(n)]
    elif layout == "top_bottom":
        ch = ff.even((H - g * (n - 1)) / n)
        cells = [(0, i * (ch + g), W, ch) for i in range(n)]
    elif layout == "grid":
        cols = 2 if n <= 4 else 3
        rows = math.ceil(n / cols)
        cw, ch = ff.even((W - g * (cols - 1)) / cols), ff.even((H - g * (rows - 1)) / rows)
        for i in range(n):
            r, c = divmod(i, cols)
            last_row_count = n - cols * (rows - 1)
            xoff = (W - (last_row_count * cw + g * (last_row_count - 1))) // 2 if r == rows - 1 and last_row_count < cols else 0
            cells.append((xoff + c * (cw + g), r * (ch + g), cw, ch))
    elif layout == "reaction":
        top_h = ff.even(H * 0.62)
        cells = [(0, 0, W, top_h)]
        rest = n - 1
        cw = ff.even((W - g * (rest - 1)) / rest)
        cells += [(i * (cw + g), top_h + g, cw, H - top_h - g) for i in range(rest)]
    elif layout == "pip":
        cells = [(0, 0, W, H)]
        pw = ff.even(W * pip_scale)
        ratio = (infos[1].get("height") or 9) / (infos[1].get("width") or 16)
        ph = ff.even(pw * ratio)
        m = int(min(W, H) * 0.04)
        for k in range(1, n):
            pos = pip_position if k == 1 else ["top_left", "top_right", "bottom_left", "bottom_right"][(k + 1) % 4]
            x = m if "left" in pos else W - pw - m
            y = m if "top" in pos else H - ph - m
            cells.append((x, y, pw, ph))
    else:
        raise ToolError(f"未知布局：{layout}")

    durs = [i["duration"] for i in infos]
    D = {"shortest": min(durs), "longest": max(durs)}.get(duration, durs[0])
    fps = round(infos[0].get("fps") or 30) or 30
    tmp = ctx.tmpdir()
    args: list[str] = []
    for p in srcs:
        args += ["-i", str(p)]
    args += ["-f", "lavfi", "-i", f"color=c={ff.color_hex_to_ffmpeg(background, 'black')}:s={W}x{H}:r={fps}:d={D:.3f}"]
    bg_idx = n
    parts = []
    for i, (x, y, cw, ch) in enumerate(cells):
        chain = f"[{i}:v]{ff.fit_filter(cw, ch, 'crop')},fps={fps}"
        if layout == "pip" and i > 0 and border:
            chain += f",pad={cw + 2 * border}:{ch + 2 * border}:{border}:{border}:color=white"
        if durs[i] < D:
            chain += f",tpad=stop_mode=clone:stop_duration={D - durs[i]:.3f}"
        if labels and i < len(labels) and labels[i]:
            chain += "," + drawtext(ctx, tmp, labels[i], size=max(24, int(min(cw, ch) * 0.06)), x="20", y="20",
                                    box=True, box_color="black@0.55", shadow=False)
        parts.append(chain + f"[c{i}]")
    cur = f"{bg_idx}:v"
    for i, (x, y, _cw, _ch) in enumerate(cells):
        if layout == "pip" and i > 0 and border:
            x, y = x - border, y - border
        parts.append(f"[{cur}][c{i}]overlay={x}:{y}:eof_action=pass[o{i}]")
        cur = f"o{i}"
    parts.append(f"[{cur}]format=yuv420p[vout]")
    has_audio = [i for i, inf in enumerate(infos) if inf["has_audio"]]
    amap: list[str] = []
    if has_audio:
        if audio == "mix" and len(has_audio) > 1:
            parts.append("".join(f"[{i}:a]" for i in has_audio) + f"amix=inputs={len(has_audio)}:duration=longest[aout]")
            amap = ["-map", "[aout]"]
        else:
            k = int(audio) if str(audio).isdigit() else 0
            if k not in has_audio:
                k = has_audio[0]
            amap = ["-map", f"{k}:a"]
    out = ctx.out_path(srcs[0], f"_{layout}", ".mp4", output=output)
    await ff.ffmpeg([*args, *(await graph_args(";".join(parts), tmp)), "-map", "[vout]", *amap, "-t", f"{D:.3f}",
                     *ff.ENC_V, *(ENC_A if amap else []), *MP4_FLAGS, str(out)],
                    progress=ctx.progress_cb(), duration=D)
    return media_result(ctx, out, size=f"{W}x{H}", duration=round(D, 2))


@tool("video_effects",
      f"给视频加滤镜/特效（可叠加多个）。可选：{', '.join(EFFECT_NAMES)}。"
      "例：cinematic 电影感(宽银幕黑边+调色)、teal_orange 青橙色调、vintage 复古胶片、cyberpunk 赛博朋克、dreamy_glow 梦幻柔光、"
      "zoom_in 缓慢推镜、shake 镜头晃动、glitch 故障风、boomerang 来回播放、lut 自定义 LUT(.cube)。可用 start/end 只处理一段。",
      obj(["input", "effects"], input=s("输入视频"),
          effects=arr({"type": ["string", "object"]}, "特效名列表，或 {name, strength(0.5~2), file(lut)}"),
          start=time_s("只截取这一段再处理（可选）"), end=time_s("结束（可选）"), output=s("输出路径，可省略")),
      category="creative")
async def video_effects(ctx: ToolContext, input: str, effects: list[Any], start: Any = None, end: Any = None,
                        output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    w, h = ff.even(info.get("width") or 1920), ff.even(info.get("height") or 1080)
    fps = round(info.get("fps") or 30) or 30
    a = parse_time(start, 0.0) or 0.0
    b = parse_time(end, info["duration"]) or info["duration"]
    dur = b - a
    names = [e if isinstance(e, str) else str(e.get("name")) for e in effects]
    if any(n in ("reverse", "boomerang") for n in names) and dur > 60:
        raise ToolError("reverse/boomerang 需要把整段载入内存，请先截取 60 秒以内的片段")
    chain = effects_chain(effects, w, h, fps, dur, "fx", ctx)
    args = ["-ss", f"{a:.3f}", "-t", f"{dur:.3f}", "-i", str(src), "-filter_complex",
            f"[0:v]scale={w}:{h},setsar=1,{chain},format=yuv420p[v]", "-map", "[v]"]
    aud: list[str] = []
    if info["has_audio"]:
        af = []
        if "reverse" in names:
            af.append("areverse")
        if "fade_in" in names:
            af.append("afade=t=in:st=0:d=0.6")
        if "fade_out" in names:
            af.append(f"afade=t=out:st={max(0, dur - 0.8):.2f}:d=0.8")
        if "boomerang" in names:
            aud = []
        else:
            aud = ["-map", "0:a"] + (["-af", ",".join(af)] if af else []) + ENC_A
    out = ctx.out_path(src, "_fx", ".mp4", output=output)
    await ff.ffmpeg([*args, *aud, *ff.ENC_V, *MP4_FLAGS, str(out)], progress=ctx.progress_cb(),
                    duration=dur * (2 if "boomerang" in names else 1))
    return media_result(ctx, out, effects=names)


@tool("video_add_text",
      "在视频上叠加文字（标题、花字、说明、倒计时等），支持动画：fade 淡入淡出 / slide_up 上滑 / slide_down / slide_left / typewriter 打字机 / none。",
      obj(["input", "texts"], input=s("输入视频"),
          texts=arr(any_of_obj("{text, start, end, position: top/upper/center/lower/bottom(+_left/_right), x, y, "
                               "size, color, box:true 背景框, box_color, border, animation, style:'title'}"), "文字列表"),
          output=s("输出路径，可省略")), category="creative")
async def video_add_text(ctx: ToolContext, input: str, texts: list[dict[str, Any]], output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    w, h = info.get("width") or 1920, info.get("height") or 1080
    tmp = ctx.tmpdir()
    filters = text_filters(ctx, tmp, texts, w, h, info["duration"])
    if not filters:
        raise ToolError("没有有效的文字")
    out = ctx.out_path(src, "_text", ".mp4", output=output)
    await ff.ffmpeg(["-i", str(src), "-vf", ",".join(filters), *ff.ENC_V, *(["-c:a", "copy"] if info["has_audio"] else []),
                     *MP4_FLAGS, str(out)], progress=ctx.progress_cb(), duration=info["duration"])
    return media_result(ctx, out, texts=len(filters))


@tool("add_overlay",
      "在视频上叠加图片或视频：Logo/水印、贴纸、边框、绿幕素材(chroma_key 抠绿)。可设置位置、大小、透明度、出现时间。",
      obj(["input", "overlay"], input=s("主视频"), overlay=s("叠加的图片(png 支持透明)或视频"),
          position=s("top_left/top_right/bottom_left/bottom_right/center，默认 top_right"),
          scale=num("叠加物宽度占主画面宽度的比例，默认 0.18"), opacity=num("不透明度 0~1，默认 1"),
          start=time_s("出现时间"), end=time_s("消失时间"), chroma_key=s("抠像颜色，如 green / 0x00FF00（绿幕素材用）"),
          output=s("输出路径，可省略")), category="creative")
async def add_overlay(ctx: ToolContext, input: str, overlay: str, position: str = "top_right", scale: float = 0.18,
                      opacity: float = 1.0, start: Any = None, end: Any = None, chroma_key: str | None = None,
                      output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    ov = ctx.resolve(overlay, must_exist=True)
    info = await ff.probe(src)
    W = info.get("width") or 1920
    ow = ff.even(W * scale)
    m = int(min(W, info.get("height") or 1080) * 0.04)
    xy = {"top_left": (f"{m}", f"{m}"), "top_right": (f"W-w-{m}", f"{m}"), "bottom_left": (f"{m}", f"H-h-{m}"),
          "bottom_right": (f"W-w-{m}", f"H-h-{m}"), "center": ("(W-w)/2", "(H-h)/2")}.get(position, (f"W-w-{m}", f"{m}"))
    is_img = ov.suffix.lower() in ff.IMAGE_EXTS
    args = ["-i", str(src)] + (["-loop", "1", "-i", str(ov)] if is_img else ["-stream_loop", "-1", "-i", str(ov)])
    chain = f"[1:v]scale={ow}:-2,format=rgba"
    if chroma_key:
        key = "0x00FF00" if chroma_key.lower() == "green" else ("0x0000FF" if chroma_key.lower() == "blue" else chroma_key)
        chain += f",colorkey={key}:0.3:0.15"
    if opacity < 1:
        chain += f",colorchannelmixer=aa={opacity}"
    enable = ""
    a, b = parse_time(start, None), parse_time(end, None)
    if a is not None or b is not None:
        enable = f":enable='between(t,{a or 0:.3f},{b if b is not None else 1e9:.3f})'"
    graph = f"{chain}[ov];[0:v][ov]overlay={xy[0]}:{xy[1]}:shortest=1{enable},format=yuv420p[v]"
    out = ctx.out_path(src, "_overlay", ".mp4", output=output)
    await ff.ffmpeg([*args, "-filter_complex", graph, "-map", "[v]", "-map", "0:a?", "-t", f"{info['duration']:.3f}",
                     *ff.ENC_V, "-c:a", "copy", *MP4_FLAGS, str(out)], progress=ctx.progress_cb(),
                    duration=info["duration"])
    return media_result(ctx, out)


@tool("add_music",
      "给视频配背景音乐：自动循环/截断到视频长度，淡入淡出，duck=true 时有人声会自动压低音乐（自动闪避）。也可 replace=true 替换原声。",
      obj(["input", "music"], input=s("视频"), music=s("音乐文件"), volume=num("音乐音量，默认 0.25"),
          original_volume=num("原声音量，默认 1.0"), music_start=time_s("从音乐的第几秒开始"),
          fade_in=num("淡入秒数，默认 1"), fade_out=num("淡出秒数，默认 2"), duck=boolean("人声闪避，默认 true"),
          replace=boolean("替换原声"), output=s("输出路径，可省略")), category="audio")
async def add_music(ctx: ToolContext, input: str, music: str, volume: float = 0.25, original_volume: float = 1.0,
                    music_start: Any = 0, fade_in: float = 1.0, fade_out: float = 2.0, duck: bool = True,
                    replace: bool = False, output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    mp = ctx.resolve(music, must_exist=True)
    info = await ff.probe(src)
    D = info["duration"]
    ms = parse_time(music_start, 0.0) or 0.0
    args = ["-i", str(src), "-stream_loop", "-1", "-ss", f"{ms:.3f}", "-i", str(mp)]
    fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    bgm = f"[1:a]{fmt},atrim=0:{D:.3f},asetpts=PTS-STARTPTS,volume={volume}"
    if fade_in:
        bgm += f",afade=t=in:st=0:d={fade_in}"
    if fade_out:
        bgm += f",afade=t=out:st={max(0.0, D - fade_out):.3f}:d={fade_out}"
    parts = [bgm + "[bgm]"]
    if info["has_audio"] and not replace:
        parts.append(f"[0:a]{fmt},volume={original_volume}[orig]")
        if duck:
            parts += ["[orig]asplit=2[o1][o2]",
                      "[bgm][o2]sidechaincompress=threshold=0.03:ratio=8:attack=30:release=400[bgmd]",
                      "[o1][bgmd]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]"]
        else:
            parts.append("[orig][bgm]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    else:
        parts.append("[bgm]anull[a]")
    out = ctx.out_path(src, "_music", ".mp4", output=output)
    await ff.ffmpeg([*args, "-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[a]", "-t", f"{D:.3f}",
                     "-c:v", "copy", *ENC_A, *MP4_FLAGS, str(out)], progress=ctx.progress_cb(), duration=D)
    return media_result(ctx, out)


@tool("audio_beats", "分析音乐节拍：返回 BPM、每一拍的时间点、强拍(每小节第一拍)、明显的鼓点/重音、最响的位置(高潮)。用于卡点剪辑。",
      obj(["input"], input=s("音乐或视频文件"), max_seconds=num("最多分析多少秒，默认 300")), category="audio")
async def audio_beats(ctx: ToolContext, input: str, max_seconds: float = 300) -> dict[str, Any]:
    try:
        import numpy  # noqa: F401
    except ImportError as e:
        raise ToolError("需要 numpy：pip install numpy") from e
    from ..media.beats import analyze, load_mono

    src = ctx.resolve(input, must_exist=True)
    y = await load_mono(src, max_seconds=max_seconds)
    import asyncio

    res = await asyncio.to_thread(analyze, y)
    beats = res["beats"]
    res["beat_count"] = len(beats)
    if len(beats) > 160:
        res["beats"] = beats[:160]
        res["note"] = "仅返回前 160 拍；可用 bpm 推算之后的节拍"
    res["onsets"] = res["onsets"][:120]
    return res


@tool("audio_process",
      "音频处理（可组合，按顺序执行）：normalize 响度标准化(-14 LUFS，适合各平台)、denoise 降噪、voice_enhance 人声增强、"
      "volume 音量、fade 淡入淡出、pitch 变调(半音)、speed 变速、echo 回声、telephone 电话音、bass_boost 低音增强、"
      "trim_silence 去掉首尾静音。视频文件会保留画面。",
      obj(["input", "operations"], input=s("音频或视频"),
          operations=arr({"type": ["string", "object"]},
                         "操作列表，如 ['denoise','voice_enhance','normalize'] 或 [{name:'pitch',semitones:3},{name:'volume',value:1.5},"
                         "{name:'fade',in:1,out:2},{name:'speed',value:1.2}]"),
          output=s("输出路径，可省略")), category="audio")
async def audio_process(ctx: ToolContext, input: str, operations: list[Any], output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    if not info["has_audio"]:
        raise ToolError("该文件没有音轨")
    D = info["duration"]
    chain: list[str] = []
    speed = 1.0
    for op in operations:
        o = {"name": op} if isinstance(op, str) else dict(op)
        n = str(o.get("name") or o.get("type") or "").lower()
        if n == "normalize":
            chain.append(f"loudnorm=I={o.get('lufs', -14)}:TP=-1.0:LRA=11")
        elif n == "denoise":
            chain.append(f"afftdn=nf={o.get('strength', -25)}")
        elif n == "voice_enhance":
            chain += ["highpass=f=80", "lowpass=f=14000", "equalizer=f=3000:t=q:w=1.2:g=3",
                      "acompressor=threshold=-18dB:ratio=3:attack=5:release=60:makeup=2"]
        elif n == "volume":
            chain.append(f"volume={o.get('value', o.get('db', 1.0))}" + ("dB" if "db" in o else ""))
        elif n == "fade":
            if o.get("in"):
                chain.append(f"afade=t=in:st=0:d={o['in']}")
            if o.get("out"):
                chain.append(f"afade=t=out:st={max(0.0, D / speed - float(o['out'])):.3f}:d={o['out']}")
        elif n == "pitch":
            k = 2 ** (float(o.get("semitones", 2)) / 12)
            chain += [f"asetrate=48000*{k:.5f}", "aresample=48000", ff.atempo_chain(1 / k)]
        elif n == "speed":
            sp = float(o.get("value", 1.0))
            speed *= sp
            chain.append(ff.atempo_chain(sp))
        elif n == "echo":
            chain.append("aecho=0.8:0.85:60|120:0.35|0.2")
        elif n == "telephone":
            chain += ["highpass=f=400", "lowpass=f=3200", "acompressor=threshold=-20dB:ratio=4"]
        elif n == "bass_boost":
            chain.append(f"bass=g={o.get('gain', 6)}:f=110")
        elif n == "trim_silence":
            chain.append("silenceremove=start_periods=1:start_threshold=-45dB:stop_periods=1:stop_threshold=-45dB"
                         ":detection=peak,areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse")
        else:
            raise ToolError(f"未知音频操作：{n}")
    chain.insert(0, "aformat=sample_fmts=fltp:sample_rates=48000")
    out = ctx.out_path(src, "_audio", output=output)
    args = ["-i", str(src)]
    if info["has_video"] and out.suffix.lower() not in ff.AUDIO_EXTS:
        if speed != 1.0:
            args += ["-filter_complex", f"[0:v]setpts=PTS/{speed}[v];[0:a]{','.join(chain)}[a]", "-map", "[v]",
                     "-map", "[a]", *ff.ENC_V]
        else:
            args += ["-af", ",".join(chain), "-c:v", "copy"]
        args += [*ENC_A, *MP4_FLAGS]
    else:
        args += ["-vn", "-af", ",".join(chain)]
        if out.suffix.lower() == ".mp3":
            args += ["-c:a", "libmp3lame", "-q:a", "2"]
        elif out.suffix.lower() == ".wav":
            args += ["-c:a", "pcm_s16le"]
    await ff.ffmpeg([*args, str(out)], progress=ctx.progress_cb(), duration=D / speed)
    return media_result(ctx, out)


@tool("audio_visualize",
      "把音频做成可视化视频（播客/音乐分享常用的“音频波形视频”）：style=wave 波形 / bars 频谱柱 / cqt 彩色频谱 / circle 圆形示波器；"
      "可加背景图(自动模糊铺满)和标题。",
      obj(["input"], input=s("音频文件"), style=s("wave / bars / cqt / circle", enum=["wave", "bars", "cqt", "circle"]),
          background=s("背景图片路径或颜色（如 #101418）"), title=s("标题文字"), subtitle=s("副标题"),
          color=s("波形颜色，默认 white"), size=s("尺寸，默认 1080x1080"), output=s("输出路径，可省略")),
      category="creative")
async def audio_visualize(ctx: ToolContext, input: str, style: str = "wave", background: str | None = None,
                          title: str | None = None, subtitle: str | None = None, color: str = "white",
                          size: str | None = None, output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    if not info["has_audio"]:
        raise ToolError("没有音轨")
    W, H = ff.parse_size(size, size, fallback=(1080, 1080))
    D = info["duration"]
    tmp = ctx.tmpdir()
    args = ["-i", str(src)]
    bg_is_img = background and not background.startswith("#") and ctx.resolve(background).exists()
    if bg_is_img:
        args += ["-loop", "1", "-framerate", "30", "-i", str(ctx.resolve(background))]
        bg = ff.blur_fit_graph("1:v", "bg0", W, H, tag="bgv") + ";[bg0]eq=brightness=-0.15[bg]"
    else:
        c = ff.color_hex_to_ffmpeg(background or "#101418")
        args += ["-f", "lavfi", "-i", f"color=c={c}:s={W}x{H}:r=30"]
        bg = "[1:v]null[bg]"
    col = ff.color_hex_to_ffmpeg(color)
    vw, vh = ff.even(W * 0.86), ff.even(H * 0.32)
    if style == "bars":
        viz = f"[0:a]showfreqs=s={vw}x{vh}:mode=bar:ascale=sqrt:fscale=log:win_size=2048:colors={col},format=rgba,colorkey=black:0.1:0.1[viz]"
    elif style == "cqt":
        viz = f"[0:a]showcqt=s={vw}x{vh}:bar_g=2:sono_h=0:axis=0:fps=30,format=rgba,colorkey=black:0.08:0.1[viz]"
    elif style == "circle":
        side = ff.even(min(W, H) * 0.55)
        vw = vh = side
        viz = (f"[0:a]avectorscope=s={side}x{side}:zoom=1.4:draw=line:rc=255:gc=255:bc=255:rf=0:gf=0:bf=0:"
               f"mode=lissajous_xy,format=rgba,colorkey=black:0.1:0.1[viz]")
    else:
        viz = f"[0:a]showwaves=s={vw}x{vh}:mode=cline:rate=30:colors={col}:scale=sqrt,format=rgba,colorkey=black:0.1:0.1[viz]"
    y_pos = "(H-h)/2" if style == "circle" else "H*0.62-h/2"
    tf = []
    if title:
        tf.append(drawtext(ctx, tmp, title, size=int(min(W, H) * 0.07), x="(w-text_w)/2", y=str(int(H * 0.12))))
    if subtitle:
        tf.append(drawtext(ctx, tmp, subtitle, size=int(min(W, H) * 0.04), color="#D0D0D0", x="(w-text_w)/2",
                           y=str(int(H * 0.12 + min(W, H) * 0.1)), bold=False))
    graph = f"{bg};{viz};[bg][viz]overlay=(W-w)/2:{y_pos}:shortest=1" + ("," + ",".join(tf) if tf else "") + ",format=yuv420p[v]"
    out = ctx.out_path(src, "_visual", ".mp4", output=output)
    await ff.ffmpeg([*args, "-filter_complex", graph, "-map", "[v]", "-map", "0:a", "-t", f"{D:.3f}", *ff.ENC_V,
                     *ENC_A, *MP4_FLAGS, str(out)], progress=ctx.progress_cb(), duration=D)
    return media_result(ctx, out, size=f"{W}x{H}")


@tool("make_cover", "生成封面图：用视频某一帧或图片作底图，加大标题/副标题（带描边或色块），自动压暗底部便于阅读。",
      obj(["source", "title"], source=s("视频或图片"), title=s("大标题（可用 \\n 换行）"), subtitle=s("副标题"),
          time=time_s("从视频的哪一秒取帧，默认 1"), size=s("尺寸/比例，默认与源相同，如 1080x1920、3:4"),
          title_color=s("标题颜色，默认 #FFE14D"), style=s("outline 描边 / block 色块", enum=["outline", "block"]),
          position=s("标题位置 top/center/bottom，默认 bottom"),
          font=s("字体：黑体 / 雅黑 / 等线 / 细体 / 楷体 / 宋体 / 仿宋，默认雅黑"),
          text_color=s("block 色块样式里的文字颜色，默认黑色"), output=s("输出路径(.jpg/.png)，可省略")),
      category="creative")
async def make_cover(ctx: ToolContext, source: str, title: str, subtitle: str | None = None, time: Any = 1,
                     size: str | None = None, title_color: str = "#FFE14D", style: str = "outline",
                     position: str = "bottom", font: str | None = None, text_color: str = "black",
                     output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(source, must_exist=True)
    info = await ff.probe(src)
    W, H = ff.parse_size(size, size, fallback=(ff.even(info.get("width") or 1080), ff.even(info.get("height") or 1920)))
    tmp = ctx.tmpdir()
    inp = ["-loop", "1", "-i", str(src)] if src.suffix.lower() in ff.IMAGE_EXTS else \
        ["-ss", f"{parse_time(time, 1.0):.3f}", "-i", str(src)]
    tsize = int(min(W, H) * 0.11)
    title = title.replace("\\n", "\n")
    lines = title.count("\n") + 1
    block_h = lines * (tsize + 8)
    ty = {"top": f"{int(H * 0.08)}", "center": "(h-text_h)/2", "bottom": f"h-text_h-{int(H * 0.16)}"}.get(position, "h*0.7")
    filters = [ff.fit_filter(W, H, "crop")]
    if position == "bottom":
        filters.append(f"drawbox=x=0:y={int(H * 0.55)}:w=iw:h={int(H * 0.45)}:color=black@0.35:t=fill")
    ffont = ff.font_file(font, True) if font else None
    if style == "block":
        filters.append(drawtext(ctx, tmp, title, size=tsize, color=text_color, x="(w-text_w)/2", y=ty, box=True,
                                box_color=ff.color_hex_to_ffmpeg(title_color) + "@0.95", shadow=False, font=ffont))
    else:
        filters.append(drawtext(ctx, tmp, title, size=tsize, color=title_color, x="(w-text_w)/2", y=ty,
                                border=max(4, tsize // 10), border_color="black", font=ffont))
    if subtitle:
        sy = f"{int(H * 0.08) + block_h + 30}" if position == "top" else f"h-{int(H * 0.09)}-text_h"
        if position == "center":
            sy = f"(h+{block_h})/2+30"
        filters.append(drawtext(ctx, tmp, subtitle, size=int(tsize * 0.45), color="white", x="(w-text_w)/2", y=sy,
                                border=3, font=ffont))
    out = ctx.out_path(src, "_cover", ".jpg", output=output)
    await ff.ffmpeg([*inp, "-frames:v", "1", "-vf", ",".join(filters), "-q:v", "2", str(out)])
    return media_result(ctx, out, size=f"{W}x{H}", images=[str(out)])


TTS_VOICES = {k: v["desc"] for k, v in EDGE_PERSONAS.items()}


@tool("tts",
      "AI 配音：把文案转成语音，并自动生成对齐的字幕(.srt)。按设置自动选择引擎：Edge 神经语音(免费) → 火山引擎豆包语音"
      "(剪映/抖音同款音色) → 声音克隆(GPT-SoVITS，本地) → OpenAI 兼容语音 API → 系统语音。"
      f"voice 可填热门音色名：{'；'.join(f'{k}（{v}）' for k, v in TTS_VOICES.items())}；"
      "也可填 Edge 音色 ID（如 zh-CN-YunxiNeural）或火山 voice_type。不填则用设置里的默认音色。"
      "长文案建议按段落分别配音，再用 video_compose 的 voiceover 对齐画面。",
      obj(["text"], text=s("要朗读的文案"), voice=s("音色：热门音色名 / Edge 音色 ID / 火山 voice_type"),
          rate=s("语速，如 +10% / -5%"), pitch=s("音调，如 +5Hz（仅 Edge）"), output=s("输出 mp3 路径，可省略"),
          max_chars=integer("字幕每行最大字数，默认 16")), category="audio")
async def tts(ctx: ToolContext, text: str, voice: str | None = None, rate: str | None = None, pitch: str | None = None,
              output: str | None = None, max_chars: int = 16) -> dict[str, Any]:
    from ..media import tts as engines

    if not text.strip():
        raise ToolError("文案为空")
    stem = re.sub(r"[^\w一-鿿]+", "_", text[:16]).strip("_") or "tts"
    out = ctx.out_path(Path(f"voice_{stem}.mp3"), "", ".mp3", output=output)
    await ctx.progress(0.05, "合成语音")
    try:
        result = await engines.synthesize(text, out, ctx.settings, voice=voice, rate=rate, pitch=pitch)
    except engines.TTSError as e:
        raise ToolError(str(e)) from e
    info = await ff.probe(out)
    cues = engines.cues_for(text, result, info["duration"], max_chars)
    srt = out.with_suffix(".srt")
    if cues:
        sublib.save(srt, cues)
    res: dict[str, Any] = {"ok": True, "output": ctx.rel(out), "subtitle": ctx.rel(srt) if cues else None,
                           "duration": info["duration"], "engine": result["engine"],
                           "subtitle_timing": "精确(逐词)" if result.get("words") else "估算",
                           "files": [str(out)] + ([str(srt)] if cues else [])}
    if result.get("errors"):
        res["fallback_reason"] = result["errors"]
        if result["engine"].startswith("系统语音"):
            res["warning"] = "当前用的是系统自带机器音，效果较差。建议告诉用户在 设置 → 语音 配置火山引擎或声音克隆。"
    return res
