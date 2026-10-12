"""Subtitle tools: speech recognition, reading/editing cues, re-segmentation, translation helpers, burn-in."""
from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from typing import Any

from ..media import asr
from ..media import ffmpeg as ff
from ..media import subs as sublib
from ..media.ffmpeg import ENC_V, MP4_FLAGS, parse_time
from .registry import ToolContext, ToolError, any_of_obj, arr, boolean, integer, num, obj, s, tool
from .video import media_result

CJK_LANGS = {"zh", "ja", "ko", "yue"}


def _sub_out(ctx: ToolContext, media: Path, output: str | None, ext: str = ".srt") -> Path:
    if output:
        p = ctx.resolve(output)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    try:
        media.resolve().relative_to(ctx.workspace.resolve())
        target = media.with_suffix(ext)
        if target == media:
            target = media.with_name(media.stem + "_sub" + ext)
        return target
    except ValueError:
        d = ctx.workspace / "subtitles"
        d.mkdir(exist_ok=True)
        return d / (media.stem + ext)


def _fmt_cues(cues: list[sublib.Cue], start_index: int = 1) -> str:
    return "\n".join(f"{i} | {sublib.ts(c.start)} → {sublib.ts(c.end)} | {c.text.replace(chr(10), ' / ')}"
                     for i, c in enumerate(cues, start_index))


@tool("transcribe",
      "语音识别生成字幕：把视频/音频里的语音转成带时间轴的 .srt 字幕（同时保存逐词时间，便于后续重新断句）。"
      "完成后建议用 subtitle_read 检查并修正错别字、专有名词。",
      obj(["input"], input=s("视频或音频文件"), language=s("语言代码：zh / en / ja / ko / auto（默认 auto）"),
          model=s("本地模型：tiny/base/small/medium/large-v3/large-v3-turbo（默认用设置里的）"),
          max_chars=integer("每条字幕最大字数（中文默认 16，英文默认 42）"),
          prompt=s("提示词：可写入专有名词/人名帮助识别，如『本期嘉宾：张三，产品：AutoStudio』"),
          strip_punctuation=boolean("去掉标点：句中逗号变空格、句末标点删除（中文短视频常用）"),
          output=s("输出 srt 路径，可省略（默认与视频同名）")), category="subtitle")
async def transcribe(ctx: ToolContext, input: str, language: str = "auto", model: str | None = None,
                     max_chars: int | None = None, prompt: str | None = None, strip_punctuation: bool = False,
                     output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(input, must_exist=True)
    info = await ff.probe(src)
    if not info["has_audio"]:
        raise ToolError("该文件没有音轨")
    cfg = ctx.settings.get("asr") or {}
    tmp = ctx.tmpdir()
    await ctx.progress(0.01, "提取音频")
    engine = cfg.get("engine", "local")
    if engine == "api":
        audio = tmp / "audio.mp3"
        await ff.ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio)])
        pid = cfg.get("api_provider") or ""
        pcfg = (ctx.settings.get("providers") or {}).get(pid) or {}
        if not pcfg.get("base_url") or not pcfg.get("api_key"):
            raise ToolError("语音识别设置为 API 模式，但没有配置可用的提供方（设置 → 语音识别）")
        await ctx.progress(0.1, "上传并识别中")
        result = await asr.transcribe_api(str(audio), base_url=pcfg["base_url"], api_key=pcfg["api_key"],
                                          model=cfg.get("api_model") or "whisper-1", language=language, prompt=prompt)
    else:
        if not asr.faster_whisper_available():
            raise ToolError("本地语音识别需要安装 faster-whisper：pip install faster-whisper。"
                            "或在 设置 → 语音识别 中改用 API 模式。")
        audio = tmp / "audio.wav"
        await ff.ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)])
        loop = asyncio.get_running_loop()
        cancel = threading.Event()

        def prog(f: float, text: str) -> None:
            loop.call_soon_threadsafe(lambda: loop.create_task(ctx.progress(0.05 + 0.93 * f, text)))

        try:
            result = await asyncio.to_thread(
                asr.transcribe_local, str(audio), model=model or cfg.get("model") or "small",
                device=cfg.get("device") or "auto", language=language, prompt=prompt,
                hf_endpoint=cfg.get("hf_endpoint") or "", progress=prog, cancel=cancel)
        except asyncio.CancelledError:
            cancel.set()
            raise
    lang = (result.get("language") or language or "").lower()
    lang = {"chinese": "zh", "english": "en", "japanese": "ja", "korean": "ko"}.get(lang, lang)
    words = result.get("words") or []
    cjk = lang in CJK_LANGS
    mc = max_chars or (16 if cjk else 42)
    if words:
        cues = sublib.segment_words(words, max_chars=mc, strip_punct=strip_punctuation)
    else:
        cues = sublib.split_long_cues([sublib.Cue(sg["start"], sg["end"], sg["text"]) for sg in result["segments"]], mc)
    if not cues:
        raise ToolError("没有识别到语音内容")
    out = _sub_out(ctx, src, output)
    sublib.save(out, cues)
    if words:
        sublib.save_words(out, words)
    preview = _fmt_cues(cues[:12])
    return {"ok": True, "subtitle": ctx.rel(out), "language": lang, "cues": len(cues),
            "duration": round(info["duration"], 2), "preview": preview, "files": [str(out)],
            "editor": {"subtitle": str(out), "media": str(src)}}


@tool("subtitle_read", "读取字幕文件（srt/vtt/ass），返回编号、时间轴和文本。长字幕可分段读取，或用 query 搜索。",
      obj(["path"], path=s("字幕文件"), start_index=integer("从第几条开始（1 开始）"), count=integer("读取条数，默认 200"),
          query=s("只返回包含该文字的字幕（可选）")), category="subtitle")
async def subtitle_read(ctx: ToolContext, path: str, start_index: int = 1, count: int = 200,
                        query: str | None = None) -> dict[str, Any]:
    p = ctx.resolve(path, must_exist=True)
    cues = sublib.load(p)
    if query:
        lines = [f"{i} | {sublib.ts(c.start)} → {sublib.ts(c.end)} | {c.text}" for i, c in enumerate(cues, 1)
                 if query.lower() in c.text.lower()]
        return {"total": len(cues), "matches": len(lines), "cues": "\n".join(lines[:300])}
    s0 = max(1, int(start_index))
    chunk = cues[s0 - 1: s0 - 1 + max(1, int(count))]
    return {"total": len(cues), "range": [s0, s0 + len(chunk) - 1], "cues": _fmt_cues(chunk, s0)}


@tool("subtitle_edit",
      "编辑字幕：批量修改指定编号的文字或时间、删除、插入、合并。编号以 subtitle_read 返回的为准（操作按原编号定位，一次提交即可）。",
      obj(["path", "operations"], path=s("字幕文件"),
          operations=arr(any_of_obj(
              "{action:'update', index, text?, start?, end?} | {action:'delete', index} | "
              "{action:'insert', start, end, text} | {action:'merge', indexes:[3,4]}"), "操作列表"),
          output=s("另存为（可选，默认覆盖原文件）")), category="subtitle")
async def subtitle_edit(ctx: ToolContext, path: str, operations: list[dict[str, Any]],
                        output: str | None = None) -> dict[str, Any]:
    p = ctx.resolve(path, must_exist=True)
    cues: list[sublib.Cue | None] = list(sublib.load(p))
    n = len(cues)
    inserts: list[sublib.Cue] = []
    changed = 0

    def get(i: Any) -> int:
        k = int(i)
        if not 1 <= k <= n:
            raise ToolError(f"编号 {k} 超出范围 1~{n}")
        return k - 1

    for op in operations:
        act = op.get("action", "update")
        if act == "update":
            k = get(op["index"])
            c = cues[k]
            if c is None:
                continue
            if "text" in op and op["text"] is not None:
                c.text = str(op["text"])
            if op.get("start") is not None:
                c.start = parse_time(op["start"]) or 0.0
            if op.get("end") is not None:
                c.end = parse_time(op["end"]) or c.end
            changed += 1
        elif act == "delete":
            cues[get(op["index"])] = None
            changed += 1
        elif act == "insert":
            inserts.append(sublib.Cue(parse_time(op["start"]) or 0.0, parse_time(op["end"]) or 0.0, str(op.get("text", ""))))
            changed += 1
        elif act == "merge":
            idx = sorted(get(i) for i in op.get("indexes") or [])
            group = [cues[i] for i in idx if cues[i] is not None]
            if len(group) >= 2:
                sep = "" if sublib.is_cjk_text("".join(g.text for g in group)) else " "
                merged = sublib.Cue(group[0].start, group[-1].end, sep.join(g.text for g in group))
                cues[idx[0]] = merged
                for i in idx[1:]:
                    cues[i] = None
                changed += 1
        else:
            raise ToolError(f"未知操作：{act}")
    final = [c for c in cues if c is not None] + inserts
    final.sort(key=lambda c: c.start)
    out = ctx.resolve(output) if output else p
    sublib.save(out, final)
    return {"ok": True, "subtitle": ctx.rel(out), "changed": changed, "total": len(final), "files": [str(out)]}


@tool("subtitle_replace", "字幕批量查找替换（修正反复出现的错别字/专有名词），支持正则。",
      obj(["path", "replacements"], path=s("字幕文件"),
          replacements=arr(any_of_obj("{find, replace, regex?:false}"), "替换规则列表"),
          output=s("另存为（可选）")), category="subtitle")
async def subtitle_replace(ctx: ToolContext, path: str, replacements: list[dict[str, Any]],
                           output: str | None = None) -> dict[str, Any]:
    p = ctx.resolve(path, must_exist=True)
    cues = sublib.load(p)
    counts = {}
    for r in replacements:
        find, rep = str(r.get("find", "")), str(r.get("replace", ""))
        if not find:
            continue
        total = 0
        for c in cues:
            if r.get("regex"):
                c.text, k = re.subn(find, rep, c.text)
            else:
                k = c.text.count(find)
                c.text = c.text.replace(find, rep)
            total += k
        counts[find] = total
    out = ctx.resolve(output) if output else p
    sublib.save(out, cues)
    return {"ok": True, "subtitle": ctx.rel(out), "replaced": counts, "files": [str(out)]}


@tool("subtitle_shift", "整体平移字幕时间（音画不同步时用），可只平移某个编号范围；也可整体缩放（帧率不一致时）。",
      obj(["path", "offset"], path=s("字幕文件"), offset=num("平移秒数，正数延后，负数提前"),
          from_index=integer("起始编号（可选）"), to_index=integer("结束编号（可选）"),
          scale=num("时间缩放系数（可选），如 1.001"), output=s("另存为（可选）")), category="subtitle")
async def subtitle_shift(ctx: ToolContext, path: str, offset: float, from_index: int | None = None,
                         to_index: int | None = None, scale: float | None = None,
                         output: str | None = None) -> dict[str, Any]:
    p = ctx.resolve(path, must_exist=True)
    cues = sublib.load(p)
    a = (from_index or 1) - 1
    b = to_index or len(cues)
    for c in cues[a:b]:
        if scale:
            c.start *= scale
            c.end *= scale
        c.start = max(0.0, c.start + offset)
        c.end = max(c.start + 0.1, c.end + offset)
    out = ctx.resolve(output) if output else p
    sublib.save(out, cues)
    return {"ok": True, "subtitle": ctx.rel(out), "shifted": b - a, "files": [str(out)]}


@tool("subtitle_apply_texts",
      "按编号批量写入新文字并保持时间轴不变——用于翻译字幕、改写、做双语字幕。mode=replace 替换原文；"
      "mode=bilingual 生成双语（原文在上、新文字在下，或 new_first=true 反过来）。建议另存为新文件。",
      obj(["path", "texts"], path=s("原字幕文件"),
          texts=arr(any_of_obj("{index, text}"), "新文字列表；也可直接传与字幕条数相同的字符串数组"),
          mode=s("replace / bilingual", enum=["replace", "bilingual"]), new_first=boolean("双语时新文字在上"),
          output=s("输出路径，如 xxx.en.srt / xxx.bilingual.srt")), category="subtitle")
async def subtitle_apply_texts(ctx: ToolContext, path: str, texts: list[Any], mode: str = "replace",
                               new_first: bool = False, output: str | None = None) -> dict[str, Any]:
    p = ctx.resolve(path, must_exist=True)
    cues = sublib.load(p)
    mapping: dict[int, str] = {}
    for i, t in enumerate(texts, 1):
        if isinstance(t, dict):
            mapping[int(t.get("index", i))] = str(t.get("text", ""))
        else:
            mapping[i] = str(t)
    for idx, text in mapping.items():
        if 1 <= idx <= len(cues):
            c = cues[idx - 1]
            if mode == "bilingual":
                c.text = f"{text}\n{c.text}" if new_first else f"{c.text}\n{text}"
            else:
                c.text = text
    out = ctx.resolve(output) if output else p.with_name(p.stem + (".bilingual" if mode == "bilingual" else ".new") + p.suffix)
    sublib.save(out, cues)
    missing = len(cues) - len([k for k in mapping if 1 <= k <= len(cues)])
    return {"ok": True, "subtitle": ctx.rel(out), "updated": len(mapping), "untouched": max(0, missing), "files": [str(out)]}


@tool("subtitle_resegment", "重新断句：调整每条字幕的最大字数（如短视频每行 12 字）。有逐词时间时会精确重排，否则按比例拆分过长的字幕。",
      obj(["path", "max_chars"], path=s("字幕文件"), max_chars=integer("每条最大字数"),
          strip_punctuation=boolean("去掉标点（句中逗号变空格、句末标点删除）"), output=s("另存为（可选）")), category="subtitle")
async def subtitle_resegment(ctx: ToolContext, path: str, max_chars: int, strip_punctuation: bool = False,
                             output: str | None = None) -> dict[str, Any]:
    p = ctx.resolve(path, must_exist=True)
    words = sublib.load_words(p)
    if words:
        cues = sublib.segment_words(words, max_chars=max_chars, strip_punct=strip_punctuation)
        note = "按逐词时间重新断句（注意：之前对文字的手动修改会被识别原文覆盖）"
    else:
        cues = sublib.split_long_cues(sublib.load(p), max_chars)
        if strip_punctuation:
            for c in cues:
                c.text = sublib.strip_punctuation(c.text)
        note = "按比例拆分"
    out = ctx.resolve(output) if output else p
    sublib.save(out, cues)
    return {"ok": True, "subtitle": ctx.rel(out), "cues": len(cues), "method": note, "files": [str(out)]}


@tool("subtitle_convert", "字幕格式转换：srt / vtt / ass / txt(纯文本稿)。",
      obj(["path", "format"], path=s("字幕文件"), format=s("目标格式", enum=["srt", "vtt", "ass", "txt"]),
          output=s("输出路径（可选）")), category="subtitle")
async def subtitle_convert(ctx: ToolContext, path: str, format: str, output: str | None = None) -> dict[str, Any]:
    p = ctx.resolve(path, must_exist=True)
    cues = sublib.load(p)
    out = ctx.resolve(output) if output else p.with_suffix("." + format)
    sublib.save(out, cues, style={"font": ff.default_font_name()})
    return {"ok": True, "output": ctx.rel(out), "files": [str(out)]}


@tool("subtitle_burn",
      "把字幕烧录（硬字幕）到视频画面上，可自定义样式：字体、字号、颜色、描边、背景框、位置、动画(pop 弹出/fade 淡入)、"
      "双语第二行高亮色。默认样式为白字黑边，适配横竖屏。",
      obj(["video", "subtitle"], video=s("视频"), subtitle=s("字幕文件 srt/vtt/ass"),
          style=any_of_obj("{font, font_size, color:'#FFFFFF', outline_color, outline, bold, box:true, box_color, "
                           "position: bottom/top/middle, margin_v, animation: pop/fade, highlight_color: 双语第二行颜色}"),
          output=s("输出路径，可省略")), category="subtitle")
async def subtitle_burn(ctx: ToolContext, video: str, subtitle: str, style: dict[str, Any] | None = None,
                        output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(video, must_exist=True)
    sp = ctx.resolve(subtitle, must_exist=True)
    info = await ff.probe(src)
    w, h = info.get("width") or 1920, info.get("height") or 1080
    tmp = ctx.tmpdir()
    st = dict(style or {})
    st.setdefault("font", ff.default_font_name())
    if (h > w) and "margin_v" not in st and st.get("position", "bottom") == "bottom":
        st["margin_v"] = int(h * 0.18)  # vertical video: keep clear of the app UI at the bottom
    ass = tmp / "burn.ass"
    if sp.suffix.lower() in (".ass", ".ssa") and not style:
        ass.write_bytes(sp.read_bytes())
    else:
        sublib.save(ass, sublib.load(sp), width=w, height=h, style=st)
    fd = ff.fonts_dir()
    vf = f"ass={ff.filter_escape_path(ass)}" + (f":fontsdir={ff.filter_escape_path(fd)}" if fd else "")
    out = ctx.out_path(src, "_subbed", ".mp4", output=output)
    await ff.ffmpeg(["-i", str(src), "-vf", vf, *ENC_V, *(["-c:a", "copy"] if info["has_audio"] else []), *MP4_FLAGS,
                     str(out)], progress=ctx.progress_cb(), duration=info["duration"])
    return media_result(ctx, out)


@tool("subtitle_embed", "把字幕作为可开关的软字幕轨封装进视频（不重新编码，速度快；播放器可选择显示）。",
      obj(["video", "subtitle"], video=s("视频"), subtitle=s("字幕文件"), language=s("语言代码，如 chi / eng"),
          output=s("输出路径（.mp4 或 .mkv），可省略")), category="subtitle")
async def subtitle_embed(ctx: ToolContext, video: str, subtitle: str, language: str = "chi",
                         output: str | None = None) -> dict[str, Any]:
    src = ctx.resolve(video, must_exist=True)
    sp = ctx.resolve(subtitle, must_exist=True)
    out = ctx.out_path(src, "_softsub", output=output)
    codec = "mov_text" if out.suffix.lower() in (".mp4", ".mov", ".m4v") else "srt"
    await ff.ffmpeg(["-i", str(src), "-i", str(sp), "-map", "0", "-map", "1", "-c", "copy", "-c:s", codec,
                     "-metadata:s:s:0", f"language={language}", str(out)])
    return media_result(ctx, out)
