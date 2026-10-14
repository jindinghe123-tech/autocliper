"""Editable timeline projects — the agent's low-level editing surface.

A project is a JSON document (workspace/projects/<name>/timeline.json) holding:
  video     main picture track, played in order (clips, stills, colour cards; per-clip in/out, speed, fit, effects,
            transition to the next clip)
  texts     free text layers at absolute times (any font, position, size, colour, box, animation)
  overlays  pictures/videos on top (picture-in-picture, stickers, logos) with position, size, opacity
  audio     any number of clips — voice, music, sound effects — each with its own time, trim, volume and fades;
            music ducks under voice; voice clips with a sidecar .srt become the subtitles
plus the creative brief, render history and which renders were actually watched.
The agent edits it with small operations, inspects it, renders previews, watches them and revises; delivery is
gated by the brief and automatic quality checks (see tools/editing.py).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .media import ffmpeg as ff
from .media import subs as sublib

BRIEF_FIELDS: dict[str, tuple[str, int]] = {
    "audience": ("给谁看、他们为什么会停下来", 6),
    "message": ("看完要记住的一句话", 6),
    "format": ("形式：口播解说 / 卡点混剪 / 情景短剧 / 对比测评 / 清单盘点 / 教程 / 文字动效 / vlog / 其它", 4),
    "duration": ("目标时长（秒）", 1),
    "structure": ("结构和节奏：分几段、每段讲什么多长、哪里加速、哪里停顿留白", 25),
    "visual": ("视觉语言：版式、色调、字体（为什么选它）、文字怎么出现、镜头运动、转场", 25),
    "sound": ("声音设计：配音的语气、音乐风格和进出点、音效放在哪里、哪里安静", 15),
    "hook": ("前 3 秒具体怎么抓人（画面 + 声音 + 文字）", 10),
    "ending": ("结尾怎么收：总结 / 反转 / 提问 / 悬念", 6),
    "not_generic": ("这条视频和套路化模板视频的区别在哪里，为什么只适合这个题材", 25),
}
FORBIDDEN_ON_SCREEN = re.compile(r"https?://|www\.|\.(com|cn|net|org)\b|来源[:：]|据.{0,10}报道|参考资料|免责声明|"
                                 r"本视频(由|为)|AI ?生成|以上(信息|内容)")
KINDS = {"video": "v", "texts": "t", "overlays": "o", "audio": "a"}
POS_KEYWORDS = ("top_left", "top_right", "bottom_left", "bottom_right", "center", "top", "bottom", "left", "right")


class TimelineError(ValueError):
    pass


def project_dir(workspace: Path, name: str) -> Path:
    slug = re.sub(r"[^\w一-鿿-]+", "_", name).strip("_")[:40] or "project"
    return workspace / "projects" / slug


def load(workspace: Path, name: str) -> tuple[Path, dict[str, Any]]:
    d = project_dir(workspace, name)
    f = d / "timeline.json"
    if not f.exists():
        raise TimelineError(f"项目「{name}」不存在，先用 project_open 创建")
    return d, json.loads(f.read_text("utf-8"))


def save(d: Path, p: dict[str, Any]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "timeline.json.tmp"
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(d / "timeline.json")


def new_project(name: str, width: int, height: int, fps: int) -> dict[str, Any]:
    return {"version": 1, "name": name, "width": width, "height": height, "fps": fps, "rev": 0, "brief": None,
            "video": [], "texts": [], "overlays": [], "audio": [], "subtitles": {"enabled": True, "style": {}},
            "counters": {}, "renders": [], "delivered": None, "created": time.time()}


# ---------------------------------------------------------------------------
# timing

def clip_duration(c: dict[str, Any]) -> float:
    if c.get("src") and c.get("kind") == "video":
        a = float(c.get("in") or 0)
        b = float(c["out"]) if c.get("out") is not None else float(c.get("src_duration") or a + 3)
        return max(0.04, (b - a) / float(c.get("speed") or 1))
    return float(c.get("duration") or 3)


def trans_of(c: dict[str, Any]) -> tuple[str, float]:
    t = c.get("transition")
    if not t or t in ("cut", "none"):
        return "none", 0.0
    if isinstance(t, str):
        return t, 0.4
    return str(t.get("type") or "fade"), float(t.get("duration") or 0.4)


def main_track_times(p: dict[str, Any]) -> tuple[list[tuple[float, float]], float]:
    """[(start, end)] of every main-track clip on the timeline, and the picture length."""
    out, t = [], 0.0
    clips = p["video"]
    for i, c in enumerate(clips):
        d = clip_duration(c)
        out.append((t, t + d))
        overlap = trans_of(c)[1] if i < len(clips) - 1 else 0.0
        overlap = min(overlap, d - 0.05, clip_duration(clips[i + 1]) - 0.05) if i < len(clips) - 1 else 0.0
        t = t + d - max(0.0, overlap)
    return out, (out[-1][1] if out else 0.0)


def audio_span(a: dict[str, Any]) -> tuple[float, float]:
    at = float(a.get("at") or 0)
    if a.get("duration") is not None:
        return at, at + float(a["duration"])
    i = float(a.get("in") or 0)
    o = float(a["out"]) if a.get("out") is not None else float(a.get("src_duration") or i + 1)
    return at, at + max(0.0, o - i)


def text_span(x: dict[str, Any]) -> tuple[float, float]:
    at = float(x.get("at") or 0)
    if x.get("end") is not None:
        return at, float(x["end"])
    return at, at + float(x.get("duration") or 2)


def total_length(p: dict[str, Any]) -> float:
    _t, pic = main_track_times(p)
    ends = [pic] + [audio_span(a)[1] for a in p["audio"] if a.get("role") == "voice"]
    return max(ends) if ends else 0.0


# ---------------------------------------------------------------------------
# inspection

def _fmt(t: float) -> str:
    return f"{t:5.1f}"


def _name(path: str | None) -> str:
    return Path(path).name[:34] if path else ""


def check(p: dict[str, Any]) -> list[str]:
    """Problems a careful editor would notice before rendering."""
    warn: list[str] = []
    times, pic = main_track_times(p)
    if not p["video"]:
        warn.append("画面轨是空的")
    for c in p["video"]:
        if c.get("src") and not Path(c["src"]).exists():
            warn.append(f"{c['id']} 的文件不存在：{c['src']}")
        if c.get("kind") == "video" and c.get("out") is not None and c.get("src_duration") and float(c["out"]) > float(c["src_duration"]) + 0.05:
            warn.append(f"{c['id']} 出点 {c['out']}s 超过素材长度 {c['src_duration']:.2f}s")
    voices = sorted((audio_span(a) + (a["id"],) for a in p["audio"] if a.get("role") == "voice"), key=lambda x: x[0])
    for (a0, a1, ida), (b0, _b1, idb) in zip(voices, voices[1:]):
        if b0 < a1 - 0.05:
            warn.append(f"配音 {ida} 和 {idb} 重叠了 {a1 - b0:.1f}s")
        elif b0 - a1 > 1.5:
            warn.append(f"{_fmt(a1)}–{_fmt(b0)} 两段配音之间空了 {b0 - a1:.1f}s（是故意留白吗？）")
    if voices and voices[-1][1] > pic + 0.05:
        warn.append(f"配音持续到 {voices[-1][1]:.1f}s，但画面只有 {pic:.1f}s")
    for a in p["audio"]:
        if a.get("src") and not Path(a["src"]).exists():
            warn.append(f"{a['id']} 的文件不存在")
        s0, s1 = audio_span(a)
        if s0 >= pic + 0.05 and pic:
            warn.append(f"{a['id']} 在画面结束之后才开始")
    if p["video"] and not any(a.get("role") == "music" for a in p["audio"]):
        warn.append("没有背景音乐（如果是有意为之可以忽略）")
    for x in p["texts"]:
        s0, s1 = text_span(x)
        n = len(re.sub(r"\s", "", x.get("text") or ""))
        if s1 - s0 < 0.4 + n / 8:
            warn.append(f"文字 {x['id']}「{x['text'][:10]}」{n} 个字只显示 {s1 - s0:.1f}s，读不完")
        if max(len(ln) for ln in (x.get("text") or " ").replace("\\n", "\n").split("\n")) > 14:
            warn.append(f"文字 {x['id']} 一行超过 14 个字，竖屏放不下或太挤，考虑换行")
        if s0 >= pic and pic:
            warn.append(f"文字 {x['id']} 在画面结束之后")
        if FORBIDDEN_ON_SCREEN.search(x.get("text") or ""):
            warn.append(f"文字 {x['id']} 含网址 / 来源 / 声明之类的内容，不应出现在画面里")
        if p["height"] > p["width"] and (x.get("position") or "").startswith("bottom"):
            warn.append(f"文字 {x['id']} 放在底部，会被平台的标题和按钮挡住（竖屏底部约 15%）")
    for o in p["overlays"]:
        if o.get("src") and not Path(o["src"]).exists():
            warn.append(f"{o['id']} 的文件不存在")
    brief = p.get("brief") or {}
    try:
        target = float(re.findall(r"\d+(?:\.\d+)?", str(brief.get("duration") or ""))[0])
        total = total_length(p)
        if total and abs(total - target) / target > 0.35:
            warn.append(f"总长 {total:.1f}s，和创意方案里的目标 {target:.0f}s 差得比较多")
    except (IndexError, ValueError, ZeroDivisionError):
        pass
    return warn


def view(p: dict[str, Any]) -> str:
    times, pic = main_track_times(p)
    total = total_length(p)
    lines = [f"项目「{p['name']}」 {p['width']}x{p['height']} {p['fps']}fps · 第 {p['rev']} 版 · 总长 {total:.1f}s · "
             f"创意方案：{'已写' if p.get('brief') else '未写（渲染前必须先写 project_brief）'}"]
    lines.append("\n画面轨（按顺序播放）")
    if not p["video"]:
        lines.append("  （空）")
    for (s0, s1), c in zip(times, p["video"]):
        if c.get("kind") == "video":
            src = f"{_name(c['src'])} {float(c.get('in') or 0):.2f}→{float(c['out']) if c.get('out') is not None else float(c.get('src_duration') or 0):.2f}"
        elif c.get("src"):
            src = f"图 {_name(c['src'])}" + (f" 运镜 {c['kenburns']}" if c.get("kenburns") else "")
        else:
            src = f"色卡 {c.get('color')}"
        extra = [f"×{c['speed']}" if c.get("speed") not in (None, 1, 1.0) else "", c.get("fit") or "",
                 "+".join(e if isinstance(e, str) else e.get("name", "") for e in c.get("effects") or []),
                 f"原声{c['volume']}" if c.get("volume") else ""]
        tn, td = trans_of(c)
        lines.append(f"  {c['id']:>4} {_fmt(s0)}–{_fmt(s1)}  {src}  {' '.join(x for x in extra if x)}"
                     f"  → {'硬切' if tn == 'none' else f'{tn} {td}s'}" + (f"  # {c['note']}" if c.get("note") else ""))
    if p["texts"]:
        lines.append("\n文字层")
        for x in sorted(p["texts"], key=lambda x: float(x.get("at") or 0)):
            s0, s1 = text_span(x)
            look = " ".join(str(v) for v in (x.get("position") or (f"({x.get('x')},{x.get('y')})"), x.get("font") or "",
                                             x.get("size") or "", x.get("color") or "", "色块" if x.get("box") else "",
                                             x.get("animation") or "") if v)
            lines.append(f"  {x['id']:>4} {_fmt(s0)}–{_fmt(s1)}  「{(x.get('text') or '').replace(chr(10), '/')[:24]}」  {look}")
    if p["overlays"]:
        lines.append("\n叠加层")
        for o in p["overlays"]:
            lines.append(f"  {o['id']:>4} {_fmt(float(o.get('at') or 0))}–{_fmt(float(o.get('at') or 0) + float(o.get('duration') or 0))}"
                         f"  {_name(o.get('src'))}  位置 {o.get('position') or (o.get('x'), o.get('y'))} 宽 {o.get('width')}"
                         f" 不透明 {o.get('opacity', 1)}")
    if p["audio"]:
        lines.append("\n音频")
        for a in sorted(p["audio"], key=lambda a: float(a.get("at") or 0)):
            s0, s1 = audio_span(a)
            fades = f" 淡入{a['fade_in']}" if a.get("fade_in") else ""
            fades += f" 淡出{a['fade_out']}" if a.get("fade_out") else ""
            lines.append(f"  {a['id']:>4} {_fmt(s0)}–{_fmt(s1)}  {a.get('role', 'sfx'):5}  {_name(a.get('src'))}"
                         f"  音量{a.get('volume', 1)}{fades}" + (f"  「{a['text'][:20]}」" if a.get("text") else ""))
    sub = p.get("subtitles") or {}
    lines.append(f"\n字幕：{'开（来自配音）' if sub.get('enabled', True) else '关'} {json.dumps(sub.get('style') or {}, ensure_ascii=False)}")
    warn = check(p)
    if warn:
        lines.append("\n需要注意：")
        lines += [f"  ⚠ {w}" for w in warn]
    if p.get("renders"):
        r = p["renders"][-1]
        lines.append(f"\n最近渲染：{r['kind']} 第 {r['rev']} 版 → {r['path']}" +
                     ("（之后又改过，需要重新渲染）" if r["rev"] != p["rev"] else "") +
                     f"；已看过 {len(r.get('watched') or [])} 处")
    return "\n".join(lines)


def subtitle_cues(p: dict[str, Any]) -> list[sublib.Cue]:
    cues: list[sublib.Cue] = []
    for a in p["audio"]:
        if a.get("role") != "voice" or not a.get("src"):
            continue
        srt = Path(a["src"]).with_suffix(".srt")
        if not srt.exists():
            continue
        s0, s1 = audio_span(a)
        off = s0 - float(a.get("in") or 0)
        for c in sublib.load(srt):
            st, en = c.start + off, c.end + off
            if en > s0 and st < s1:
                cues.append(sublib.Cue(max(st, s0), min(en, s1), c.text))
    return sorted(cues, key=lambda c: c.start)


def note_watch(path: Path, start: float, end: float) -> None:
    """Called by video_watch: remember which parts of a project render the agent has looked at."""
    try:
        renders_dir = path.resolve().parent
        if renders_dir.name != "renders":
            return
        d = renders_dir.parent
        f = d / "timeline.json"
        if not f.exists():
            return
        p = json.loads(f.read_text("utf-8"))
        for r in p.get("renders", []):
            if Path(r["path"]).resolve() == path.resolve():
                r.setdefault("watched", []).append([round(start, 2), round(end, 2)])
        save(d, p)
    except Exception:  # noqa: BLE001 - bookkeeping must never break watching
        pass


def to_clip(c: dict[str, Any]) -> dict[str, Any]:
    """Main-track item → video_compose clip dict."""
    out: dict[str, Any] = {k: c[k] for k in ("fit", "bg", "effects") if c.get(k) is not None}
    if c.get("kind") == "video":
        out.update(src=c["src"], start=float(c.get("in") or 0), speed=float(c.get("speed") or 1),
                   volume=float(c.get("volume") or 0))
        out["end"] = float(c["out"]) if c.get("out") is not None else float(c.get("src_duration") or 0) or None
    elif c.get("src"):
        out.update(src=c["src"], duration=clip_duration(c), kenburns=c.get("kenburns") or "none")
    else:
        out.update(color=c.get("color") or "#000000", duration=clip_duration(c))
    return out
