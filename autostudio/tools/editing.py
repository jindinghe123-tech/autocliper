"""Low-level editing: plan, brief, timeline projects (edit / view / render), voice lines, fonts, and the delivery gate.

The agent is the editor. These tools only execute exactly what it asks for; they never make creative choices.
Two guard rails keep the work honest: rendering needs a written creative brief, and `deliver` refuses until the
final cut is up to date, has actually been watched, and passes automatic quality checks.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any

from .. import timeline as tl
from ..media import ffmpeg as ff
from ..media import fonts as fontlib
from ..media import subs as sublib
from ..media import tts as engines
from .creative import EFFECT_NAMES, TRANSITIONS, _render_clip, join_clips, make_cover, text_filters
from .registry import ToolContext, ToolError, any_of_obj, arr, boolean, integer, num, obj, s, tool
from .video import graph_args

FITS = ("crop", "blur", "pad", "stretch")
ROLES = ("voice", "music", "sfx")
FIELDS = {
    "video": ("src", "color", "lavfi", "in", "out", "duration", "speed", "fit", "bg", "effects", "kenburns", "volume", "transition",
              "vf", "note"),
    "texts": ("text", "at", "duration", "end", "position", "x", "y", "size", "color", "font", "box", "box_color", "border",
              "border_color", "animation", "alpha", "bold"),
    "overlays": ("src", "at", "duration", "in", "position", "x", "y", "width", "opacity", "fade", "fps", "vf"),
    "audio": ("src", "at", "in", "out", "duration", "volume", "fade_in", "fade_out", "role", "duck", "af", "text"),
}
ADD_OPS = {"add_clip": "video", "add_text": "texts", "add_overlay": "overlays", "add_audio": "audio"}


# ---------------------------------------------------------------------------
# plan

@tool("plan_update",
      "维护这次任务的工作清单（界面上会实时显示）。开始一个多步骤任务时先列出全部步骤，之后每完成或开始一步就更新状态。"
      "清单要具体到这个任务（例如“写口播稿：5 段，每段一个信息点”），不要写空泛的步骤。",
      obj(["items"], items=arr(any_of_obj("{text: 步骤, status: todo / doing / done / skipped}"), "完整的清单（每次都传全部）"),
          note=s("一句话说明现在在做什么、下一步是什么")),
      category="system")
async def plan_update(ctx: ToolContext, items: list[dict[str, Any]], note: str = "") -> dict[str, Any]:
    marks = {"done": "✓", "doing": "▶", "skipped": "–"}
    rows = [f"{marks.get(str(i.get('status')), '○')} {i.get('text', '')}" for i in items if isinstance(i, dict)]
    left = sum(1 for i in items if isinstance(i, dict) and i.get("status") not in ("done", "skipped"))
    return {"plan": rows, "remaining": left, "note": note}


# ---------------------------------------------------------------------------
# project & brief

def _load(ctx: ToolContext, name: str) -> tuple[Path, dict[str, Any]]:
    try:
        return tl.load(ctx.workspace, name)
    except tl.TimelineError as e:
        raise ToolError(str(e)) from e


@tool("project_open",
      "创建或打开一个剪辑项目（时间线工程）。项目里有：画面主轨、文字层、叠加层（画中画/贴图/logo）、任意多条音频（配音/音乐/音效），"
      "全部由你用 timeline_edit 逐条编辑。已存在则直接打开并返回当前时间线。",
      obj(["name"], name=s("项目名"), aspect=s("画面比例：9:16 / 16:9 / 1:1 / 4:5 / 3:4，默认 9:16"),
          size=s("或直接给分辨率，如 1080x1920"), fps=integer("帧率，默认 30")),
      category="creative")
async def project_open(ctx: ToolContext, name: str, aspect: str = "9:16", size: str | None = None, fps: int = 30) -> dict[str, Any]:
    d = tl.project_dir(ctx.workspace, name)
    if (d / "timeline.json").exists():
        _d, p = _load(ctx, name)
        return {"project": name, "folder": ctx.rel(d), "opened": True, "timeline": tl.view(p)}
    w, h = ff.parse_size(size, aspect, fallback=(1080, 1920))
    p = tl.new_project(name, w, h, int(fps or 30))
    tl.save(d, p)
    return {"project": name, "folder": ctx.rel(d), "created": True, "timeline": tl.view(p),
            "next": "先用 project_brief 写下创作意图，再动手。"}


@tool("project_brief",
      "用你自己的话写下这条视频的创作意图（自由书写，没有固定格式）：想让谁看、看完记住什么、做成什么形式、"
      "画面和声音打算怎么处理、为什么这样处理适合这个题材。渲染前要写；想法变了随时重写。审片时总编会拿它对照成片。",
      obj(["project", "intent"], project=s("项目名"), intent=s("创作意图（Markdown 自由书写）")), category="creative")
async def project_brief(ctx: ToolContext, project: str, intent: str) -> dict[str, Any]:
    d, p = _load(ctx, project)
    text = str(intent or "").strip()
    if len(text) < 120:
        raise ToolError("创作意图写得太少了：把这条视频具体想怎么做、为什么这样做写清楚，别人读完应该能想象出成片。")
    p["brief"] = {"text": text, "at": time.time()}
    tl.save(d, p)
    (d / "创作意图.md").write_text(f"# {p['name']}\n\n{text}\n", "utf-8")
    return {"ok": True, "file": ctx.rel(d / "创作意图.md")}


# ---------------------------------------------------------------------------
# editing

def _num(v: Any, name: str, lo: float = 0, hi: float = 1e6) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError) as e:
        raise ToolError(f"{name} 必须是数字（秒）") from e
    if not lo <= x <= hi:
        raise ToolError(f"{name}={x} 超出范围")
    return x


def _find(p: dict[str, Any], iid: str) -> tuple[str, dict[str, Any]]:
    for track in tl.KINDS:
        for it in p[track]:
            if it["id"] == iid:
                return track, it
    raise ToolError(f"时间线上没有 {iid}")


async def _validate(ctx: ToolContext, track: str, it: dict[str, Any], warnings: list[str]) -> None:
    for k in ("in", "out", "duration", "at", "end", "fade_in", "fade_out", "fade"):
        if it.get(k) is not None:
            it[k] = _num(it[k], k)
    if it.get("src") and track == "overlays" and "%" in str(it["src"]):   # image sequence, e.g. frames/%04d.png
        pattern = ctx.resolve(it["src"])
        if not any(Path(str(pattern) % n).exists() for n in (0, 1)):
            raise ToolError(f"图片序列 {it['src']} 找不到第一帧")
        it["src"], it["sequence"] = str(pattern), True
    elif it.get("src"):
        path = ctx.resolve(it["src"], must_exist=True)
        it["src"] = str(path)
        if track in ("video", "overlays", "audio") and path.suffix.lower() not in ff.IMAGE_EXTS:
            info = await ff.probe(path)
            it["src_duration"] = round(float(info.get("duration") or 0), 3)
        if track == "video":
            it["kind"] = "image" if path.suffix.lower() in ff.IMAGE_EXTS else "video"
    elif track == "video" and it.get("lavfi"):
        it["kind"] = "gen"   # picture generated by an ffmpeg source filter (gradients, noise, cellauto, mandelbrot…)
    elif track == "video":
        if not it.get("color"):
            raise ToolError("画面片段需要 src（视频/图片）、color（色卡）或 lavfi（生成画面）")
        it["kind"] = "color"
    if track == "video":
        if it.get("kind") != "video" and not it.get("duration"):
            it["duration"] = 3.0
        if it.get("speed") is not None:
            it["speed"] = _num(it["speed"], "speed", 0.1, 8)
        if it.get("fit") and it["fit"] not in FITS:
            raise ToolError(f"fit 只能是 {'/'.join(FITS)}")
        for e in it.get("effects") or []:
            name = e if isinstance(e, str) else (e or {}).get("name")
            if name not in EFFECT_NAMES:
                raise ToolError(f"没有「{name}」这个特效，可选：{', '.join(EFFECT_NAMES)}")
        tn, _td = tl.trans_of(it)
        if tn != "none" and tn not in TRANSITIONS:
            raise ToolError(f"没有「{tn}」这个转场，可选：cut 或 {', '.join(TRANSITIONS)}")
        if it.get("kind") == "video" and it.get("src_duration"):
            a = float(it.get("in") or 0)
            if a >= it["src_duration"]:
                raise ToolError(f"入点 {a}s 超过了素材长度 {it['src_duration']}s")
    if track == "texts":
        if not str(it.get("text") or "").strip():
            raise ToolError("文字层需要 text")
        if it.get("at") is None:
            raise ToolError("文字层需要 at（出现时间）")
        if it.get("font") and ff.font_file(it["font"]) == ff.default_font_file(True) and it["font"] not in ("雅黑",):
            warnings.append(f"字体「{it['font']}」不可用，暂时用默认字体：用 font_list 看可用字体，或 font_get 下载")
    if track == "overlays" and (it.get("at") is None or not it.get("duration")):
        raise ToolError("叠加层需要 at 和 duration")
    if track == "audio":
        if it.get("role", "sfx") not in ROLES:
            raise ToolError(f"role 只能是 {'/'.join(ROLES)}")
        it.setdefault("role", "sfx")
        if it.get("at") is None:
            raise ToolError("音频需要 at（在时间线上的开始时间）")


@tool("timeline_edit",
      "编辑时间线：一次可以执行多条操作，时间单位都是秒。每一层都可以写原始 ffmpeg 滤镜和表达式，没有固定样式的限制。\n"
      "add_clip {src 视频/图片 | color 色卡 | lavfi 生成画面(如 'gradients=s=1080x1920:c0=0x10251f:c1=0x2e6b4f:speed=0.01'), "
      "in, out(视频入出点), duration, speed, fit: crop/blur/pad/stretch, bg, effects:[现成特效], vf: 原始滤镜链(如 'eq=contrast=1.2,"
      "curves=preset=increase_contrast'), kenburns(图片运镜), volume 原声音量(默认0), transition 到下一段: cut 或 {type, duration}, "
      "index 插入位置, note}\n"
      "add_text {text(\\n 换行), at, duration|end, position 或 x,y(数字，或随时间变化的 ffmpeg 表达式，如 "
      "'(w-text_w)/2+200*sin(t*3)'), size, color, font, box, box_color, border, border_color, animation, "
      "alpha(透明度表达式，覆盖 animation), bold}\n"
      "add_overlay {src 图片 / 视频 / 带透明通道的 .webm .mov / 图片序列 'frames/%04d.png'(配 fps), at, duration, "
      "position 或 x,y(可用表达式做运动), width, opacity, fade, vf(作用在这一层的原始滤镜)}——studio_run 做出来的动画就从这里叠上去\n"
      "add_audio {src, role: voice/music/sfx, at, in, out|duration(音乐可循环), volume, fade_in, fade_out, duck, af(原始音频滤镜，"
      "如 'aecho=0.8:0.7:60:0.3' 'highpass=f=200')}\n"
      "update {id, 字段…(null 删除)}；remove {id}；move {id, index}；split {id, at}；"
      "set {fade_in, fade_out, vf(整条片子的最终画面滤镜，如统一调色), af(整条片子的最终音频滤镜), subtitles: {enabled, style: {font, "
      "font_size, color, outline_color, outline, box, box_color, position, margin_v, animation, highlight_color}}}\n"
      f"现成特效（可选用，不是必须）：{', '.join(EFFECT_NAMES)}。转场：cut 或 {', '.join(TRANSITIONS)}。",
      obj(["project", "ops"], project=s("项目名"), ops=arr(any_of_obj("一条操作，如 {op: 'add_clip', src: 'assets/videos/a.mp4', in: 2, out: 5.5}"), "操作列表")),
      category="creative")
async def timeline_edit(ctx: ToolContext, project: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    d, p = _load(ctx, project)
    log: list[str] = []
    warnings: list[str] = []
    for n, raw in enumerate(ops or [], 1):
        op = dict(raw or {})
        kind = op.pop("op", None)
        try:
            if kind in ADD_OPS:
                track = ADD_OPS[kind]
                it = {k: v for k, v in op.items() if k in FIELDS[track] and v is not None}
                await _validate(ctx, track, it, warnings)
                prefix = tl.KINDS[track]
                p["counters"][prefix] = p["counters"].get(prefix, 0) + 1
                it["id"] = f"{prefix}{p['counters'][prefix]}"
                if track == "video" and op.get("index") is not None:
                    p[track].insert(max(0, min(len(p[track]), int(op["index"]))), it)
                else:
                    p[track].append(it)
                log.append(f"+ {it['id']}")
            elif kind == "update":
                track, it = _find(p, str(op.pop("id", "")))
                new = dict(it)
                for k, v in op.items():
                    if k not in FIELDS[track]:
                        raise ToolError(f"{track} 没有字段 {k}")
                    if v is None:
                        new.pop(k, None)
                    else:
                        new[k] = v
                if "src" in op:
                    new.pop("src_duration", None)
                await _validate(ctx, track, new, warnings)
                it.clear()
                it.update(new)
                log.append(f"~ {it['id']}")
            elif kind == "remove":
                track, it = _find(p, str(op.get("id", "")))
                p[track].remove(it)
                log.append(f"- {it['id']}")
            elif kind == "move":
                track, it = _find(p, str(op.get("id", "")))
                if track != "video":
                    raise ToolError("move 只用于画面主轨；其它轨道改 at 即可")
                p[track].remove(it)
                p[track].insert(max(0, min(len(p[track]), int(op.get("index") or 0))), it)
                log.append(f"↕ {it['id']}")
            elif kind == "split":
                track, it = _find(p, str(op.get("id", "")))
                if track != "video":
                    raise ToolError("split 只用于画面主轨")
                at = _num(op.get("at"), "at", 0.05)
                dur = tl.clip_duration(it)
                if at >= dur - 0.05:
                    raise ToolError(f"切点 {at}s 超出片段长度 {dur:.2f}s")
                second = {k: v for k, v in it.items() if k != "id"}
                if it.get("kind") == "video":
                    cut = float(it.get("in") or 0) + at * float(it.get("speed") or 1)
                    it["out"], second["in"] = round(cut, 3), round(cut, 3)
                else:
                    it["duration"], second["duration"] = round(at, 3), round(dur - at, 3)
                it["transition"] = "cut"
                p["counters"]["v"] = p["counters"].get("v", 0) + 1
                second["id"] = f"v{p['counters']['v']}"
                p["video"].insert(p["video"].index(it) + 1, second)
                log.append(f"✂ {it['id']} → {it['id']}+{second['id']}")
            elif kind == "set":
                for k in ("fade_in", "fade_out"):
                    if k in op:
                        p[k] = _num(op[k], k, 0, 10)
                for k in ("vf", "af"):
                    if k in op:
                        p[k] = str(op[k]) if op[k] else None
                if "subtitles" in op:
                    sub = dict(p.get("subtitles") or {})
                    sub.update({k: v for k, v in (op["subtitles"] or {}).items() if k in ("enabled", "style")})
                    p["subtitles"] = sub
                log.append("⚙ 项目设置")
            else:
                raise ToolError(f"未知操作 {kind}，可用：{', '.join([*ADD_OPS, 'update', 'remove', 'move', 'split', 'set'])}")
        except ToolError as e:
            if log:
                p["rev"] += 1
                tl.save(d, p)
            raise ToolError(f"第 {n} 条操作（{kind}）失败：{e}。前面 {len(log)} 条已生效。") from e
    p["rev"] += 1
    tl.save(d, p)
    out: dict[str, Any] = {"ok": True, "applied": log, "rev": p["rev"], "timeline": tl.view(p)}
    if warnings:
        out["warnings"] = warnings
    return out


@tool("timeline_view", "查看时间线：每个片段/文字/叠加/音频的精确时间、素材、样式，以及自动发现的问题（配音重叠、长时间空白、文字读不完、"
      "被平台按钮挡住、画面比配音短、和目标时长不符…）。改完时间线、渲染之前都应该看一遍。",
      obj(["project"], project=s("项目名")), category="creative")
async def timeline_view(ctx: ToolContext, project: str) -> dict[str, Any]:
    _d, p = _load(ctx, project)
    return {"timeline": tl.view(p)}


# ---------------------------------------------------------------------------
# voice

@tool("voice_lines",
      "批量配音：把每一句口播分别合成为音频（并生成对应字幕），默认按顺序放到时间线的配音轨上（句间停顿 gap 秒），返回每句的时间。"
      "节奏由你决定：需要强调的句子前后可以单独调 at 留白，也可以给某一句换音色或语速。",
      obj(["project", "lines"], project=s("项目名"),
          lines=arr(any_of_obj("一句口播：字符串，或 {text, voice, rate, at(指定开始时间)}"), "口播句子"),
          voice=s("默认音色：解说男声 / 热血解说 / 新闻主播 / 温柔女声 / 元气少女 / 可爱童声 …"),
          rate=s("默认语速，如 +8%"), start=num("第一句从第几秒开始（默认接在现有配音之后）"),
          gap=num("句间停顿秒数，默认 0.25"), place=boolean("是否自动放到时间线上，默认 true")),
      category="audio")
async def voice_lines(ctx: ToolContext, project: str, lines: list[Any], voice: str | None = None, rate: str | None = None,
                      start: float | None = None, gap: float = 0.25, place: bool = True) -> dict[str, Any]:
    d, p = _load(ctx, project)
    vdir = d / "voice"
    vdir.mkdir(parents=True, exist_ok=True)
    voices = [a for a in p["audio"] if a.get("role") == "voice"]
    t = float(start) if start is not None else max((tl.audio_span(a)[1] for a in voices), default=0.0) + (gap if voices else 0)
    made, engine = [], ""
    stamp = int(time.time() * 1000) % 10**7
    for i, line in enumerate(lines or []):
        item = {"text": line} if isinstance(line, str) else dict(line)
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        await ctx.progress(i / max(1, len(lines)), f"配音 {i + 1}/{len(lines)}")
        out = vdir / f"line_{stamp}_{i + 1:02d}.mp3"
        try:
            res = await engines.synthesize(text, out, ctx.settings, voice=item.get("voice") or voice, rate=item.get("rate") or rate)
        except engines.TTSError as e:
            raise ToolError(f"配音失败：{e}") from e
        engine = res["engine"]
        dur = float((await ff.probe(out))["duration"])
        cues = engines.cues_for(text, res, dur, 13 if p["height"] > p["width"] else 18)
        if cues:
            sublib.save(out.with_suffix(".srt"), cues)
        at = float(item["at"]) if item.get("at") is not None else t
        entry: dict[str, Any] = {"text": text, "file": ctx.rel(out), "duration": round(dur, 2), "at": round(at, 2)}
        if place:
            p["counters"]["a"] = p["counters"].get("a", 0) + 1
            aid = f"a{p['counters']['a']}"
            p["audio"].append({"id": aid, "src": str(out), "role": "voice", "at": round(at, 3), "src_duration": round(dur, 3),
                               "volume": 1.0, "text": text})
            entry["id"] = aid
        made.append(entry)
        t = at + dur + float(gap)
    if place:
        p["rev"] += 1
        tl.save(d, p)
    out_d: dict[str, Any] = {"ok": True, "engine": engine, "lines": made, "voice_end": round(t - float(gap), 2)}
    if engine.startswith("系统语音"):
        out_d["warning"] = "用的是系统自带语音，效果较差：告诉用户到 设置 → 语音 下载离线配音模型或配置其它引擎。"
    return out_d


# ---------------------------------------------------------------------------
# render

def _overlay_xy(o: dict[str, Any], w: int, h: int) -> tuple[str, str]:
    m = int(min(w, h) * 0.04)
    if o.get("x") is not None and o.get("y") is not None:
        def val(v: Any) -> str:
            try:
                return str(int(float(v)))
            except (TypeError, ValueError):
                return str(v)   # expression: the layer can move, e.g. "W-w-40*t"
        return val(o["x"]), val(o["y"])
    top = m + (int(h * 0.06) if h > w else 0)
    bottom = f"H-h-{m + (int(h * 0.16) if h > w else 0)}"
    return {"top_left": (str(m), str(top)), "top_right": (f"W-w-{m}", str(top)), "bottom_left": (str(m), bottom),
            "bottom_right": (f"W-w-{m}", bottom), "top": ("(W-w)/2", str(top)), "bottom": ("(W-w)/2", bottom),
            "left": (str(m), "(H-h)/2"), "right": (f"W-w-{m}", "(H-h)/2")}.get(o.get("position") or "center",
                                                                              ("(W-w)/2", "(H-h)/2"))


async def render_project(ctx: ToolContext, d: Path, p: dict[str, Any], quality: str) -> tuple[Path, float]:
    w, h, fps = p["width"], p["height"], p["fps"]
    final = quality == "final"
    crf = 18 if final else 30
    tmp = ctx.tmpdir()
    clips = [tl.to_clip(c) for c in p["video"]]
    for i, (c, src) in enumerate(zip(clips, p["video"])):
        if src.get("kind") == "gen":   # pictures generated by an ffmpeg source filter become an ordinary clip
            gen = tmp / f"gen_{i:03d}.mp4"
            dur = tl.clip_duration(src)
            try:
                await ff.ffmpeg(["-f", "lavfi", "-i", src["lavfi"], "-t", f"{dur:.3f}", "-vf", f"scale={w}:{h},fps={fps}",
                                 "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p", str(gen)])
            except ff.FFmpegError as e:
                raise ToolError(f"{src['id']} 的 lavfi 生成失败：{str(e)[-300:]}") from e
            c.pop("color", None)
            c.update(src=str(gen), start=0, end=dur)
    trans = [tl.trans_of(c) for c in p["video"][:-1]]
    rendered = []
    for i, c in enumerate(clips):
        await ctx.progress(0.02 + 0.55 * i / len(clips), f"渲染画面 {i + 1}/{len(clips)}")
        rendered.append(await _render_clip(ctx, c, i, w, h, fps, tmp, None, crf, None))
    joined, pic = await join_clips(ctx, rendered, trans, tmp, fps, crf, ctx.progress_cb(0.58, 0.68, "拼接与转场"))
    total = max(pic, tl.total_length(p))

    args = ["-i", str(joined)]
    graph: list[str] = []
    v = "[0:v]"
    if total > pic + 0.04:   # narration runs past the picture: hold the last frame
        graph.append(f"{v}tpad=stop_mode=clone:stop_duration={total - pic:.3f}[vpad]")
        v = "[vpad]"
    idx = 1
    for k, o in enumerate(p["overlays"]):
        at, dur = float(o.get("at") or 0), float(o.get("duration") or 3)
        if o.get("sequence"):
            args += ["-framerate", str(o.get("fps") or fps), "-start_number", "0" if Path(o["src"] % 0).exists() else "1",
                     "-t", f"{dur:.3f}", "-i", o["src"]]
        elif Path(o["src"]).suffix.lower() in ff.IMAGE_EXTS:
            args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", o["src"]]
        else:
            from ..studio import _decoder, probe as sprobe   # transparent webm needs libvpx to keep its alpha
            args += [*_decoder(sprobe(o["src"])), "-ss", f"{float(o.get('in') or 0):.3f}", "-t", f"{dur:.3f}", "-i", o["src"]]
        ow = ff.even(float(o.get("width") or w * 0.35))
        op = float(o.get("opacity") if o.get("opacity") is not None else 1)
        fade = min(float(o.get("fade") if o.get("fade") is not None else 0.25), dur / 3)
        chain = f"[{idx}:v]" + (f"{o['vf']}," if o.get("vf") else "") + f"scale={ow}:-2,format=rgba,colorchannelmixer=aa={op:.3f}"
        if fade:
            chain += f",fade=t=in:st=0:d={fade:.3f}:alpha=1,fade=t=out:st={max(0.0, dur - fade):.3f}:d={fade:.3f}:alpha=1"
        graph.append(chain + f",setpts=PTS-STARTPTS+{at:.3f}/TB[ov{k}]")
        x, y = _overlay_xy(o, w, h)
        graph.append(f"{v}[ov{k}]overlay=x={x}:y={y}:enable='between(t,{at:.3f},{at + dur:.3f})':eof_action=pass[vo{k}]")
        v = f"[vo{k}]"
        idx += 1
    vparts: list[str] = []
    if p["texts"]:
        items = []
        for x in p["texts"]:
            s0, s1 = tl.text_span(x)
            items.append({"text": x["text"], "start": s0, "end": s1,
                          **{k: x[k] for k in ("position", "x", "y", "size", "color", "font", "box", "box_color", "border",
                                               "border_color", "animation", "alpha", "bold") if x.get(k) is not None}})
        vparts += text_filters(ctx, tmp, items, w, h, total)
    sub = p.get("subtitles") or {}
    cues = tl.subtitle_cues(p) if sub.get("enabled", True) else []
    if cues:
        style = dict(sub.get("style") or {})
        fd = ff.fonts_dir_for(style.get("font"))
        style["font"] = ff.font_name(style.get("font"))
        style.setdefault("font_size", round(min(w, h) * 0.065))
        style.setdefault("margin_v", round(h * (0.18 if h > w else 0.08)))
        ass = tmp / "subs.ass"
        sublib.save(ass, cues, width=w, height=h, style=style)
        vparts.append(f"ass={ff.filter_escape_path(ass)}" + (f":fontsdir={ff.filter_escape_path(fd)}" if fd else ""))
    if p.get("vf"):   # the author's own finishing chain for the whole picture
        vparts.insert(0, str(p["vf"]))
    if p.get("fade_in"):
        vparts.append(f"fade=t=in:st=0:d={p['fade_in']}")
    if p.get("fade_out"):
        vparts.append(f"fade=t=out:st={max(0.0, total - p['fade_out']):.3f}:d={p['fade_out']}")
    graph.append(f"{v}{','.join(vparts) if vparts else 'null'}[vout]")

    graph.append("[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,apad[base]")
    groups: dict[str, list[str]] = {r: [] for r in ROLES}
    for k, a in enumerate(p["audio"]):
        s0, s1 = tl.audio_span(a)
        length = s1 - s0
        if length <= 0.02:
            continue
        ain = float(a.get("in") or 0)
        loop = a.get("src_duration") and length > float(a["src_duration"]) - ain + 0.05
        args += (["-stream_loop", "-1"] if loop else []) + ["-i", a["src"]]
        chain = (f"[{idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                 f"atrim={ain:.3f}:{ain + length:.3f},asetpts=PTS-STARTPTS,volume={float(a.get('volume') if a.get('volume') is not None else 1):.3f}")
        if a.get("fade_in"):
            chain += f",afade=t=in:st=0:d={float(a['fade_in']):.3f}"
        if a.get("fade_out"):
            chain += f",afade=t=out:st={max(0.0, length - float(a['fade_out'])):.3f}:d={float(a['fade_out']):.3f}"
        if a.get("af"):   # the author's own processing for this clip
            chain += f",{a['af']}"
        ms = int(s0 * 1000)
        graph.append(chain + f",adelay={ms}|{ms}[au{k}]")
        groups[a.get("role") or "sfx"].append(f"[au{k}]")
        idx += 1

    def mix(labels: list[str], name: str) -> None:
        graph.append(f"{labels[0]}anull[{name}]" if len(labels) == 1 else
                     f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest[{name}]")

    final_in = ["[base]"]
    for r in ("voice", "sfx", "music"):
        if groups[r]:
            mix(groups[r], r)
    if groups["sfx"]:
        final_in.append("[sfx]")
    duck = groups["music"] and groups["voice"] and any(a.get("duck", True) for a in p["audio"] if a.get("role") == "music")
    if duck:
        graph.append("[voice]asplit=2[vx1][vx2]")
        graph.append("[music][vx2]sidechaincompress=threshold=0.03:ratio=8:attack=30:release=400[musicd]")
        final_in += ["[vx1]", "[musicd]"]
    else:
        final_in += [f"[{r}]" for r in ("voice", "music") if groups[r]]
    afx = f"atrim=0:{total:.3f}"
    if p.get("af"):
        afx += f",{p['af']}"
    if p.get("fade_out"):
        afx += f",afade=t=out:st={max(0.0, total - p['fade_out']):.3f}:d={p['fade_out']}"
    # platforms play everything at about -14 LUFS; normalising here is engineering, not a creative choice
    # (pure silence would make loudnorm divide by zero, so only when there is sound to normalise)
    has_sound = any(groups.values()) or any(float(c.get("volume") or 0) > 0 for c in p["video"])
    norm = "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000" if has_sound else "anull"
    graph.append(f"{''.join(final_in)}amix=inputs={len(final_in)}:normalize=0:duration=first,{afx},{norm}[aout]"
                 if len(final_in) > 1 else f"[base]{afx},{norm}[aout]")

    rdir = d / "renders"
    rdir.mkdir(exist_ok=True)
    out = rdir / f"{quality}_v{p['rev']}.mp4"
    await ctx.progress(0.7, "叠加文字、字幕和声音")
    await ff.ffmpeg([*args, *(await graph_args(";".join(graph), tmp)), "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
                     "-c:v", "libx264", "-preset", "medium" if final else "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", "192k", "-ar", "48000", *ff.MP4_FLAGS, str(out)],
                    progress=ctx.progress_cb(0.7, 1.0, "最终合成"), duration=total)
    return out, total


@tool("timeline_render",
      "渲染时间线。preview 快速预览（画质较低）；final 最终版（高画质）。需要先写 project_brief。"
      "渲染出来的东西要自己用 video_watch 看，觉得不好就改。",
      obj(["project"], project=s("项目名"), quality=s("preview / final，默认 preview", enum=["preview", "final"])),
      category="creative")
async def timeline_render(ctx: ToolContext, project: str, quality: str = "preview") -> dict[str, Any]:
    d, p = _load(ctx, project)
    if not p.get("brief"):
        raise ToolError("还没写创作意图：先用 project_brief 想清楚这条视频要做成什么样、为什么，再渲染。")
    if not p["video"]:
        raise ToolError("画面轨是空的")
    issues = tl.check(p)
    out, total = await render_project(ctx, d, p, quality)
    p["renders"].append({"rev": p["rev"], "kind": quality, "path": str(out), "duration": round(total, 2),
                         "at": time.time(), "watched": []})
    tl.save(d, p)
    return {"ok": True, "file": ctx.rel(out), "kind": quality, "duration": round(total, 2), "rev": p["rev"],
            "open_issues": issues, "files": [str(out)],
            "next": "用 video_watch 看这个文件：开头 3 秒、每个镜头切换、结尾。看完写下哪里不好，然后改。"}


# ---------------------------------------------------------------------------
# delivery gate

async def _qc(path: Path) -> tuple[list[str], dict[str, Any]]:
    fails: list[str] = []
    stats: dict[str, Any] = {}
    code, _o, err = await ff.run_proc([ff.ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(path), "-vf",
                                       "blackdetect=d=0.4:pix_th=0.10", "-af", "silencedetect=n=-45dB:d=2.5,ebur128",
                                       "-f", "null", "-"], timeout=900)
    info = await ff.probe(path)
    dur = float(info.get("duration") or 0)
    stats["duration"] = round(dur, 2)
    for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err or ""):
        a, b = float(a), float(b)
        if b < dur - 0.8 and a > 0.3:
            fails.append(f"{a:.1f}–{b:.1f}s 是黑屏")
    for a, b in re.findall(r"silence_start: ([\d.]+)[\s\S]*?silence_end: ([\d.]+)", err or ""):
        fails.append(f"{float(a):.1f}–{float(b):.1f}s 完全没有声音")
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", err or "")
    if m:
        lufs = float(m[-1])
        stats["loudness_lufs"] = lufs
        if lufs < -24:
            fails.append(f"整体音量太小（{lufs} LUFS，平台推荐约 -14）")
        elif lufs > -8:
            fails.append(f"整体音量太大，可能爆音（{lufs} LUFS）")
    return fails, stats


@tool("deliver",
      "交付成片。成片怎么做出来的都可以（时间线、studio_run 代码、ffmpeg_run……），交付关卡只看结果：\n"
      "· 这个文件（这一版）你自己用 video_watch 看过：开头、结尾，以及中间至少一处；\n"
      "· 用 review_cut 审过这一版：总编的 must_fix 都改掉了（改了就是新的一版，要重新看、重新审），"
      "不同意的必须在 responses 里逐条说明理由；\n"
      "· 自动检查：黑屏、无声、音量。\n"
      "通过后生成封面、发布文案，把成片放进交付文件夹。",
      obj(["video", "publish", "cover"], video=s("成片文件"), project=s("所属剪辑项目（可选，用于放置交付文件和取干净的封面画面）"),
          publish=any_of_obj("{title, description, tags:[...]}"),
          cover=any_of_obj("封面：{image: 你自己做好的封面图}，或 {time: 取画面的时间点, title, subtitle, font, style: block/outline, "
                           "position, title_color, text_color} 由工具合成"),
          responses=arr(s("对总编某条 must_fix 的回应：为什么不改"), "对未修改的 must_fix 逐条说明理由（可选）"),
          sources=arr(s("出处"), "事实来源和参考资料（写进发布文案，不进画面）")),
      category="creative")
async def deliver(ctx: ToolContext, video: str, publish: dict[str, Any], cover: dict[str, Any], project: str | None = None,
                  responses: list[str] | None = None, sources: list[str] | None = None) -> dict[str, Any]:
    from .workbench import cut_record

    src = ctx.resolve(video, must_exist=True)
    info = await ff.probe(src)
    dur = float(info.get("duration") or 0)
    rec = cut_record(src)
    fails: list[str] = []
    watched = rec.get("watched") or []
    if not any(a <= 0.5 for a, _b in watched):
        fails.append("你还没看过这一版的开头（video_watch 看 0~3 秒）")
    if not any(b >= dur - 1.0 for _a, b in watched):
        fails.append("你还没看过这一版的结尾")
    if len(watched) < 3:
        fails.append(f"这一版你只看了 {len(watched)} 处：开头、中间、结尾至少各看一处")
    reviews = rec.get("reviews") or []
    if not reviews:
        fails.append("这一版还没审：用 review_cut 请总编看一遍")
    else:
        must = reviews[-1].get("must_fix") or []
        if must and len([r for r in responses or [] if str(r).strip()]) < len(must):
            fails.append(f"总编对这一版提了 {len(must)} 条必须修改：改完重新渲染、重新看、重新审；"
                         "确实不该改的，在 responses 里逐条说明理由")
    qc_fails, stats = await _qc(src)
    fails += qc_fails
    d = tl.project_dir(ctx.workspace, project) if project else None
    p = None
    if d and (d / "timeline.json").exists():
        _d, p = _load(ctx, project)
        for x in p["texts"]:
            if tl.FORBIDDEN_ON_SCREEN.search(x.get("text") or ""):
                fails.append(f"文字 {x['id']}「{x['text'][:16]}」是网址 / 来源 / 声明，不应出现在画面里")
        for c in tl.subtitle_cues(p):
            if tl.FORBIDDEN_ON_SCREEN.search(c.text):
                fails.append(f"字幕「{c.text[:16]}」在念网址 / 来源 / 声明")
                break
    if not (publish or {}).get("title") or not (publish or {}).get("description"):
        fails.append("发布文案需要 title 和 description")
    if not (cover or {}).get("image") and not (cover or {}).get("title"):
        fails.append("封面需要 image（你做好的图），或 title（由工具合成）")
    if fails:
        return {"ok": False, "delivered": False, "fix_these": fails, "stats": stats}

    out_dir = d if p else ctx.workspace / "outputs" / f"{src.stem}_交付"
    out_dir.mkdir(parents=True, exist_ok=True)
    cover_path = out_dir / "封面.jpg"
    if cover.get("image"):
        await ff.ffmpeg(["-i", str(ctx.resolve(cover["image"], must_exist=True)), "-frames:v", "1", "-q:v", "2", str(cover_path)])
    else:
        frame_src, at = src, float(cover.get("time") or 1.0)
        if p:   # use the clean source picture under that moment, not the render with text burned in
            times, _pic = tl.main_track_times(p)
            for (s0, s1), c in zip(times, p["video"]):
                if s0 <= at < s1 and c.get("src"):
                    frame_src = Path(c["src"])
                    at = float(c.get("in") or 0) + (at - s0) * float(c.get("speed") or 1) if c.get("kind") == "video" else 0.0
                    break
        await make_cover(ctx, source=str(frame_src), title=cover["title"], subtitle=cover.get("subtitle"), time=at,
                         size=f"{info.get('width')}x{info.get('height')}", title_color=cover.get("title_color") or "#FFE14D",
                         style=cover.get("style") or "outline", position=cover.get("position") or "center",
                         font=cover.get("font"), text_color=cover.get("text_color") or "black", output=str(cover_path))
    video_path = out_dir / "成片.mp4"
    if src.resolve() != video_path.resolve():
        shutil.copyfile(src, video_path)
    tags = [str(t).strip().lstrip("#") for t in publish.get("tags") or [] if str(t).strip()]
    doc = [f"# {publish['title']}", "", str(publish["description"]).strip(), ""]
    if tags:
        doc += [" ".join(f"#{t}" for t in tags), ""]
    if sources:
        doc += ["## 参考来源", *[f"- {x}" for x in sources], ""]
    credit_file = ctx.workspace / "assets" / "CREDITS.md"
    if p and credit_file.exists():
        used = {str(Path(i["src"]).resolve()) for t in ("video", "overlays", "audio") for i in p[t] if i.get("src")}
        credits = []
        for line in credit_file.read_text("utf-8").splitlines():
            m = re.match(r"- `([^`]+)` — (.*?)\s*<!--", line)
            if m and str((ctx.workspace / m.group(1)).resolve()) in used:
                credits.append(m.group(2))
        if credits:
            doc += ["## 素材来源（标注「需署名」的请写进简介）", *[f"- {c}" for c in credits], ""]
    if responses:
        doc += ["## 审片意见与回应", *[f"- {r}" for r in responses], ""]
    (out_dir / "发布文案.md").write_text("\n".join(doc), "utf-8")
    if p:
        p["delivered"] = {"file": str(src), "at": time.time()}
        tl.save(d, p)
    return {"ok": True, "delivered": True, "video": ctx.rel(video_path), "cover": ctx.rel(cover_path),
            "publish_copy": ctx.rel(out_dir / "发布文案.md"), "review_score": (reviews[-1] if reviews else {}).get("score"),
            "stats": stats, "files": [str(video_path), str(cover_path)]}


# ---------------------------------------------------------------------------
# fonts

@tool("font_list", "列出可用字体：系统字体族（黑体/雅黑/等线/细体/楷体/宋体/仿宋），以及可免费下载、可商用的中文设计字体（站酷快乐体、"
      "马善政楷书、龙藏体、志莽行书、思源宋体…）及其气质。字体是视觉语言的一部分，按题材挑选。",
      obj(), category="creative")
async def font_list(ctx: ToolContext) -> dict[str, Any]:
    system = [{"name": k, "available": ff.font_file(k) != ff.default_font_file(True) or k == "雅黑"} for k in ff.FONT_FAMILIES]
    return {"system": system, "downloadable": fontlib.catalog(),
            "usage": "在 timeline_edit 的文字层 font、字幕 style.font、deliver 的 cover.font 里填名字；可下载的字体先用 font_get。"}


@tool("font_get", "下载一款免费可商用的中文设计字体（SIL OFL 授权），之后就能在文字层、字幕和封面里用它的名字。",
      obj(["name"], name=s("字体名，见 font_list")), category="creative")
async def font_get(ctx: ToolContext, name: str) -> dict[str, Any]:
    try:
        path, family = await fontlib.ensure(name)
    except (ValueError, RuntimeError) as e:
        raise ToolError(str(e)) from e
    return {"ok": True, "name": name, "family": family, "file": str(path)}
