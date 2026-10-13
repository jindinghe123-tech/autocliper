"""One-call short video production from a storyboard: the model does the creative work (topic, script, shots),
this tool does the mechanics the same way every time — voice-over, footage, timing, subtitles, music, sound
effects, render, cover and publishing copy — and delivers a complete package in its own folder."""
from __future__ import annotations

import asyncio
import re
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from ..media import ffmpeg as ff
from ..media import stock
from ..media import subs as sublib
from ..media import tts as engines
from .assets import add_credit, fetch_item
from .creative import make_cover, video_compose
from .registry import ToolContext, ToolError, any_of_obj, arr, boolean, num, obj, s, tool

KENBURNS = ["zoom_in", "pan_right", "zoom_out", "pan_left", "pan_up"]
ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "3:4": (1080, 1440)}
GAP = 0.25          # breath after each narrated line
DEFAULT_BGM = "upbeat inspiring background"
STOP = {"the", "a", "an", "of", "in", "on", "at", "and", "with", "for", "to", "from", "by", "is", "are", "close", "up",
        "shot", "view", "background", "footage", "video", "image", "photo", "stock", "hd", "4k"}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]{3,}", (text or "").lower()) if w not in STOP]


def relevance(item: dict[str, Any], query: str) -> int:
    """How many query words show up in the result's title/tags (prefix match handles plurals & -ing)."""
    hay = _words(" ".join([item.get("title") or "", " ".join(map(str, item.get("tags") or []))]))
    score = 0
    for q in _words(query):
        stem = q[:5] if len(q) > 5 else q
        if any(h.startswith(stem) or q.startswith(h[:5]) and len(h) >= 4 for h in hay):
            score += 1
    return score


def variants(query: str) -> list[str]:
    """Full query first, then shorter ones: keyless libraries match every word, so long queries often find nothing."""
    words = _words(query)   # content words only, so shortened queries never end up as "in forest"
    out = [query]
    if len(words) > 2:
        out += [" ".join(words[:2]), " ".join(words[-2:])]
    if len(words) > 1:
        out += sorted(words, key=len, reverse=True)[:1]
    seen: list[str] = []
    for q in out:
        if q.lower() not in (x.lower() for x in seen):
            seen.append(q)
    return seen[:4]


def _slug(t: str) -> str:
    return re.sub(r"[^\w一-鿿]+", "_", t).strip("_")[:24] or "video"


class Producer:
    def __init__(self, ctx: ToolContext, title: str, aspect: str):
        self.ctx = ctx
        self.cfg = dict(ctx.settings.get("assets") or {})
        self.w, self.h = ASPECTS.get(aspect, ASPECTS["9:16"])
        self.orientation = "portrait" if self.h > self.w else "landscape" if self.w > self.h else "square"
        self.dir = ctx.workspace / "outputs" / f"{_slug(title)}_{time.strftime('%m%d-%H%M')}"
        self.work = self.dir / ".work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.used: set[str] = set()
        self.credits: list[str] = []
        self.warnings: list[str] = []
        self.loose: list[str] = []

    # ---- assets ----------------------------------------------------------------
    async def resolve(self, ref: str | None, kind: str, need: float = 0.0) -> tuple[Path | None, str]:
        """Local path / search-result id / English search words → (local file, short description)."""
        ref = (ref or "").strip()
        if not ref:
            return None, ""
        try:
            p = self.ctx.resolve(ref)
            if p.is_file():
                return p, self.ctx.rel(p)
        except (ToolError, OSError, ValueError):
            pass
        item = stock.lookup(ref)
        if item is None:
            item = await self.search(ref, kind, need)
        if item is None:
            self.warnings.append(f"没找到合适的{stock.KIND_LABEL.get(kind, '素材')}：「{ref}」")
            return None, ""
        try:
            path = await fetch_item(self.ctx, self.cfg, item)
        except (ToolError, httpx.HTTPError, ff.FFmpegError) as e:
            self.warnings.append(f"素材下载失败「{item.get('title')}」：{str(e)[:80]}")
            return None, ""
        self.used.add(item["id"])
        rel = self.ctx.rel(path)
        line = add_credit(self.ctx, item, rel)
        self.credits.append(line + ("（需署名）" if item["attribution"] else ""))
        return path, f"{item['source']}：{(item.get('title') or '')[:30]}"

    async def search(self, ref: str, kind: str, need: float) -> dict[str, Any] | None:
        """Best unused, relevant result: try the kind (video falls back to image) and progressively shorter queries."""
        # pictures must match what the line talks about (audio is judged by mood); only checkable for English words
        strict = kind in ("video", "image") and bool(_words(ref))
        for k in [kind] + (["image"] if kind == "video" else []):
            for q in variants(ref) if kind != "music" else variants(ref) + [DEFAULT_BGM]:
                try:
                    items, _w = await stock.search(self.cfg, k, q, limit=12,
                                                   orientation=self.orientation if k in ("video", "image") else None,
                                                   min_duration=min(need, 15) if k == "video" and need else None,
                                                   max_duration=4 if k == "sfx" else None, instrumental=k == "music")
                except stock.StockError as e:
                    self.warnings.append(f"素材搜索失败「{q}」：{e}")
                    return None
                items = [i for i in items if i["id"] not in self.used]
                if strict:
                    scored = sorted(((relevance(i, ref), n, i) for n, i in enumerate(items)), key=lambda t: (-t[0], t[1]))
                    items = [i for sc, _n, i in scored if sc >= 1]
                elif k == "music" and need:
                    items.sort(key=lambda i: (float(i.get("duration") or 0) < need, -float(i.get("duration") or 0) / 600))
                if items:
                    if q != ref:
                        self.loose.append(f"「{ref}」→ 用「{q}」找到")
                    self.used.add(items[0]["id"])   # claim it now: shots are resolved concurrently
                    return items[0]
        return None

    # ---- narration -----------------------------------------------------------------
    async def narrate(self, i: int, text: str, voice: str | None, speed: str | None) -> tuple[Path, float, list[sublib.Cue], str]:
        out = self.work / f"voice_{i:02d}.mp3"
        try:
            res = await engines.synthesize(text, out, self.ctx.settings, voice=voice, rate=speed)
        except engines.TTSError as e:
            raise ToolError(f"配音失败：{e}") from e
        dur = float((await ff.probe(out))["duration"])
        cues = engines.cues_for(text, res, dur, 13 if self.h > self.w else 18)
        return out, dur, cues, res["engine"]

    async def last_frame(self, video: Path, i: int) -> Path:
        img = self.work / f"tail_{i:02d}.jpg"
        await ff.ffmpeg(["-sseof", "-0.2", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(img)])
        return img


@tool("short_video_make",
      "★ 按分镜脚本一键做出完整的短视频成品：配音 → 自动找素材 → 对齐时间轴 → 字幕 → 背景音乐和音效 → 合成 → 封面 → 发布文案，"
      "全部放在 outputs/<标题>_<时间>/ 里。你负责创意（选题、口播稿、每个镜头的画面和屏幕关键词），工具负责制作。"
      "visual / sfx / bgm 可以填本地文件路径、asset_search 结果的 id，或英文搜索词（工具会自动搜索可商用素材）。"
      "做完后用 video_watch 抽查开头 3 秒、镜头切换和结尾，有问题就改分镜重新生成。",
      obj(["title", "shots"],
          title=s("视频标题（用于封面和文件夹名）"),
          shots=arr(any_of_obj(
              "镜头：{narration: 这一镜的口播（口语化，一两句，决定这一镜时长）, "
              "visual: 画面 — 本地路径 / 素材 id / 英文搜索词（具体名词+动作，如 'robot dog walking in forest'）, "
              "visual_type: 'video' 或 'image'（按搜索词找素材时用，默认 video）, "
              "text: 屏幕关键词（≤12 字，可省略；第一镜写钩子）, duration: 没有口播时的秒数, "
              "sfx: 镜头开始的音效 — 路径 / id / 英文搜索词，如 'whoosh'、'pop'、'ding'}"), "分镜，按播放顺序，一般 6~14 个"),
          aspect=s("画面比例，默认 9:16 竖屏", enum=list(ASPECTS)),
          voice=s("配音音色：解说男声 / 热血解说 / 新闻主播 / 温柔女声 / 元气少女 …"),
          speed=s("语速，如 +8%（短视频一般 +5%~+12%）"),
          bgm=s("背景音乐：本地路径 / 素材 id / 英文风格词（如 'curious playful pizzicato'）；填 none 不要音乐"),
          bgm_volume=num("背景音乐音量，默认 0.14"),
          subtitles=boolean("烧录字幕，默认 true"),
          cover_title=s("封面大字，默认用 title，可用 \\n 换行"),
          publish=any_of_obj("发布文案 {title, description, tags:[...]}，写进 发布文案.md（可直接给 social_publish 用）"),
          sources=arr(s("参考资料链接或出处"), "事实来源：只写进发布文案，绝不出现在视频画面里")),
      category="creative")
async def short_video_make(ctx: ToolContext, title: str, shots: list[dict[str, Any]], aspect: str = "9:16",
                           voice: str | None = None, speed: str | None = None, bgm: str | None = None,
                           bgm_volume: float = 0.14, subtitles: bool = True, cover_title: str | None = None,
                           publish: dict[str, Any] | None = None, sources: list[str] | None = None) -> dict[str, Any]:
    shots = [({"narration": sh} if isinstance(sh, str) else dict(sh)) for sh in shots or []]
    if not shots:
        raise ToolError("shots 不能为空")
    pr = Producer(ctx, title, aspect)
    n = len(shots)

    # 1) voice-over per shot decides each shot's length
    voice_files: list[Path | None] = []
    durations: list[float] = []
    all_cues: list[list[sublib.Cue]] = []
    engine_used = ""
    for i, sh in enumerate(shots):
        await ctx.progress(0.02 + 0.25 * i / n, f"配音 {i + 1}/{n}")
        text = str(sh.get("narration") or "").strip()
        if text:
            f, d, cues, engine_used = await pr.narrate(i, text, voice, speed)
            voice_files.append(f)
            durations.append(round(d + GAP, 3))
            all_cues.append(cues)
        else:
            voice_files.append(None)
            durations.append(max(0.8, float(sh.get("duration") or 2.5)))
            all_cues.append([])
    if engine_used.startswith("系统语音"):
        pr.warnings.append("配音用的是系统自带语音，效果较差：到 设置 → 语音 下载离线配音模型或配置其它引擎。")
    total = sum(durations)
    starts = [sum(durations[:i]) for i in range(n)]

    # 2) footage, sound effects, music
    sem = asyncio.Semaphore(3)

    async def visual(i: int) -> tuple[Path | None, str]:
        async with sem:
            sh = shots[i]
            kind = "image" if str(sh.get("visual_type") or "").lower() == "image" else "video"
            return await pr.resolve(sh.get("visual") or title, kind, durations[i])

    async def sfx(i: int) -> tuple[Path | None, str]:
        async with sem:
            return await pr.resolve(shots[i].get("sfx"), "sfx")

    await ctx.progress(0.3, "寻找画面素材")
    visuals = list(await asyncio.gather(*(visual(i) for i in range(n))))
    sfx_files = list(await asyncio.gather(*(sfx(i) for i in range(n))))
    music_path = None
    if (bgm or "").strip().lower() not in ("none", "无", "不要"):
        await ctx.progress(0.42, "寻找背景音乐")
        music_path, _desc = await pr.resolve(bgm or DEFAULT_BGM, "music", total)

    # 3) timeline clips (hard cuts keep every line exactly on its picture)
    clips: list[dict[str, Any]] = []
    big = round(min(pr.w, pr.h) * 0.078)
    for i, (sh, d, (path, _desc)) in enumerate(zip(shots, durations, visuals)):
        clip: dict[str, Any]
        if path and path.suffix.lower() in ff.IMAGE_EXTS:
            clip = {"src": str(path), "duration": d, "kenburns": KENBURNS[i % len(KENBURNS)]}
            extra = None
        elif path:
            info = await ff.probe(path)
            avail = float(info.get("duration") or 0)
            extra = None
            if avail >= d + 0.2:
                st = round(min(avail - d, avail * 0.15), 3)
                clip = {"src": str(path), "start": st, "end": round(st + d, 3), "volume": 0}
            else:
                sp = max(0.6, avail / d) if avail else 1.0
                clip = {"src": str(path), "start": 0, "end": round(avail, 3), "speed": round(sp, 3), "volume": 0}
                short = d - avail / sp
                if short > 0.05:   # still too short: hold the last frame with a slow push-in
                    extra = {"src": str(await pr.last_frame(path, i)), "duration": round(short, 3), "kenburns": "zoom_in"}
        else:
            clip = {"color": "#15171d", "duration": d}
            extra = None
        if sh.get("text"):
            clip["text"] = {"text": str(sh["text"])[:24], "position": "upper", "size": int(big * (1.25 if i == 0 else 1)),
                            "color": "#FFE14D" if i == 0 else "white", "animation": "slide_up", "border": max(3, big // 14)}
        clips.append(clip)
        if extra:
            clips.append(extra)

    # 4) one speech track: every line + effect placed at its shot's start
    mix: Path | None = None
    parts = [(f, starts[i], 1.0) for i, f in enumerate(voice_files) if f] + \
            [(p, starts[i], 0.55) for i, (p, _d) in enumerate(sfx_files) if p]
    if parts:
        await ctx.progress(0.5, "混合配音和音效")
        mix = pr.work / "speech.m4a"
        args: list[str] = []
        graph = []
        for k, (f, at, vol) in enumerate(parts):
            args += ["-i", str(f)]
            ms = int(at * 1000)
            graph.append(f"[{k}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={vol},adelay={ms}|{ms}[p{k}]")
        graph.append("".join(f"[p{k}]" for k in range(len(parts))) +
                     f"amix=inputs={len(parts)}:normalize=0:duration=longest,apad,atrim=0:{total:.3f}[out]")
        await ff.ffmpeg([*args, "-filter_complex", ";".join(graph), "-map", "[out]", "-c:a", "aac", "-b:a", "192k", str(mix)])

    # 5) subtitles on the final timeline
    srt = None
    cues = [sublib.Cue(c.start + starts[i], c.end + starts[i], c.text) for i, cs in enumerate(all_cues) for c in cs]
    if cues:
        srt = pr.dir / "字幕.srt"
        sublib.save(srt, cues)

    # 6) render
    out = pr.dir / "成片.mp4"
    await ctx.progress(0.55, "合成视频")
    await video_compose(ctx, clips=clips, output=str(out), size=f"{pr.w}x{pr.h}", transition="none",
                        music={"src": str(music_path), "volume": bgm_volume, "duck": True, "fade_in": 0.5, "fade_out": 1.5}
                        if music_path else None,
                        voiceover={"src": str(mix), "volume": 1.0} if mix else None,
                        subtitles={"src": str(srt), "animation": "pop", "font_size": round(min(pr.w, pr.h) * 0.072),
                                   # keep clear of the caption / button overlay platforms draw over the bottom of vertical videos
                                   "margin_v": round(pr.h * (0.18 if pr.h > pr.w else 0.08))} if srt and subtitles else None,
                        fade_in=0.15, fade_out=0.5)
    if not out.exists():
        raise ToolError("合成失败：没有生成成片")

    # 7) cover + publishing copy + storyboard
    await ctx.progress(0.95, "生成封面和发布文案")
    cover = pr.dir / "封面.jpg"
    try:
        await make_cover(ctx, source=str(out), title=cover_title or title, time=min(1.5, durations[0] / 2),
                         output=str(cover), position="center")
    except (ToolError, ff.FFmpegError) as e:
        pr.warnings.append(f"封面生成失败：{e}")
    pub = dict(publish or {})
    tags = [str(t).strip().lstrip("#") for t in pub.get("tags") or [] if str(t).strip()]
    doc = [f"# {pub.get('title') or title}", "", (pub.get("description") or "").strip(), ""]
    if tags:
        doc += [" ".join(f"#{t}" for t in tags), ""]
    if sources:
        doc += ["## 参考来源", "（放在简介或评论区，不要出现在视频画面里）", *[f"- {x}" for x in sources], ""]
    need_credit = [c for c in pr.credits if "需署名" in c]
    if pr.credits:
        doc += ["## 素材来源", *(["发布时请把下面标注「需署名」的素材写进简介："] if need_credit else []),
                *[f"- {c}" for c in pr.credits], ""]
    doc += ["## 制作信息", f"- 时长：{total:.1f} 秒 · {pr.w}x{pr.h}", f"- 配音：{engine_used or '无'}", ""]
    publish_md = pr.dir / "发布文案.md"
    publish_md.write_text("\n".join(doc), "utf-8")
    board = ["| # | 时长 | 口播 | 画面 | 屏幕文字 |", "|---|---|---|---|---|"]
    for i, (sh, d, (_p, desc)) in enumerate(zip(shots, durations, visuals), 1):
        board.append(f"| {i} | {d:.1f}s | {(sh.get('narration') or '').strip()} | {desc or '（纯色）'} | {sh.get('text') or ''} |")
    (pr.dir / "分镜.md").write_text(f"# {title} · 分镜\n\n" + "\n".join(board) + "\n", "utf-8")
    shutil.rmtree(pr.work, ignore_errors=True)

    files = [str(out)] + ([str(cover)] if cover.exists() else [])
    if pr.loose:
        pr.warnings.append("这些镜头用放宽后的关键词找的素材，可能不够贴切，请重点检查：" + "；".join(pr.loose))
    if stock.available(pr.cfg, "video") == ["wikimedia"]:
        pr.warnings.append("目前只用了免 key 的素材库，实拍视频很少。到 设置 → 素材 免费填一个 Pexels 或 Pixabay 的 key，画面质量会好很多。")
    return {"ok": True, "folder": ctx.rel(pr.dir), "video": ctx.rel(out), "cover": ctx.rel(cover) if cover.exists() else None,
            "publish_copy": ctx.rel(publish_md), "duration": round(total, 1), "size": f"{pr.w}x{pr.h}", "voice": engine_used,
            "shots": [{"n": i + 1, "start": round(starts[i], 1), "duration": durations[i], "visual": visuals[i][1] or "（无素材，纯色）"}
                      for i in range(n)],
            "warnings": pr.warnings, "files": files,
            "next": "用 video_watch 抽查：开头 3 秒是否抓人、画面和口播是否对得上、字幕有无错字、结尾是否完整。"}
