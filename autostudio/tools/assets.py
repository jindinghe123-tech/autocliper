"""Find and download royalty-free footage, images, background music and sound effects."""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

import httpx

from ..media import ffmpeg as ff
from ..media import stock
from .registry import ToolContext, ToolError, arr, boolean, integer, num, obj, s, tool
from .video import drawtext

ASSET_DIRS = {"video": "assets/videos", "image": "assets/images", "music": "assets/music", "sfx": "assets/sfx"}
CREDITS = "assets/CREDITS.md"
MAX_BYTES = 800 * 1024 * 1024
CTYPE_EXT = {"video/mp4": "mp4", "video/webm": "webm", "video/ogg": "ogv", "video/quicktime": "mov", "image/jpeg": "jpg",
             "image/png": "png", "image/webp": "webp", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/ogg": "ogg",
             "audio/wav": "wav", "audio/x-wav": "wav", "audio/flac": "flac"}


def _cfg(ctx: ToolContext) -> dict[str, Any]:
    return dict(ctx.settings.get("assets") or {})


def _fmt_dur(d: float | None) -> str:
    if not d:
        return ""
    d = int(round(d))
    return f"{d // 60}:{d % 60:02d}"


async def _sheet(ctx: ToolContext, items: list[dict[str, Any]], cfg: dict[str, Any]) -> Path | None:
    """Numbered grid of result thumbnails, so a vision model can pick shots by looking at them."""
    tmp = ctx.tmpdir()
    sem = asyncio.Semaphore(6)
    cell = 300

    async def one(i: int, it: dict[str, Any], c: httpx.AsyncClient) -> Path | None:
        if not it.get("thumb"):
            return None
        async with sem:
            try:
                r = await c.get(it["thumb"])
                r.raise_for_status()
            except Exception:  # noqa: BLE001 - a missing thumbnail just leaves a gap
                return None
            src = tmp / f"t{i:02d}"
            src.write_bytes(r.content)
            out = tmp / f"c{i:02d}.jpg"
            meta = " ".join(x for x in (_fmt_dur(it.get("duration")),
                                        f"{it['width']}x{it['height']}" if it.get("width") else "") if x)
            vf = (f"scale={cell}:{cell}:force_original_aspect_ratio=decrease,pad={cell}:{cell}:(ow-iw)/2:(oh-ih)/2:color=0x1d1f26,"
                  + drawtext(ctx, tmp, f"#{i}", size=30, x="8", y="8", box=True, box_color="black@0.7", shadow=False)
                  + ("," + drawtext(ctx, tmp, meta, size=17, x="8", y="h-th-10", box=True, box_color="black@0.6", shadow=False)
                     if meta else ""))
            try:
                await ff.ffmpeg(["-i", str(src), "-frames:v", "1", "-vf", vf, "-q:v", "4", str(out)])
            except ff.FFmpegError:
                return None
            return out if out.exists() else None

    async with stock.client(cfg, timeout=20) as c:
        cells = [p for p in await asyncio.gather(*(one(i, it, c) for i, it in enumerate(items, 1))) if p]
    if not cells:
        return None
    seq = tmp / "seq"
    seq.mkdir()
    for j, p in enumerate(cells):
        p.replace(seq / f"{j:03d}.jpg")
    cols = 4 if len(cells) > 4 else len(cells)
    rows = -(-len(cells) // cols)
    out_dir = ctx.workspace / ".frames"
    out_dir.mkdir(exist_ok=True)
    sheet = out_dir / f"assets_{int(time.time() * 1000) % 10**8}.jpg"
    await ff.ffmpeg(["-framerate", "1", "-i", str(seq / "%03d.jpg"), "-vf",
                     f"tile={cols}x{rows}:padding=6:margin=6:color=white", "-frames:v", "1", "-q:v", "3", str(sheet)])
    return sheet if sheet.exists() else None


@tool("asset_search",
      "搜索可免费使用的素材：video 实拍视频画面（空镜、B-roll）、image 图片、music 背景音乐 BGM、sfx 音效（转场 whoosh、"
      "点击 click、叮 ding、爆炸 boom、笑声、环境声…）。英文关键词效果最好（中文主题先译成 2~4 个英文词，搜不到就换同义词）。"
      "video / image 会返回一张带编号的缩略图拼图，看图挑选；music 看时长、曲风标签挑选。"
      "默认只返回可商用、可修改的授权。选好后用 asset_download 下载。",
      obj(["query", "kind"],
          query=s("搜索关键词，例如 'city night timelapse'、'upbeat happy ukulele'、'whoosh transition'"),
          kind=s("素材类型", enum=list(stock.KINDS)),
          limit=integer("返回数量，默认 8，最多 16"),
          orientation=s("画面方向（video/image）：竖屏短视频选 portrait", enum=["landscape", "portrait", "square"]),
          min_duration=num("最短时长（秒）"), max_duration=num("最长时长（秒）"),
          instrumental=boolean("music：只要纯音乐（无人声），做口播/解说的背景音乐时建议开启"),
          allow_noncommercial=boolean("也返回仅限非商业使用的素材（只在用户明确是个人非商业用途时开启）"),
          sources=arr(s("素材库", enum=list(stock.PROVIDERS)), "只搜这些素材库（一般不用填）")),
      category="assets")
async def asset_search(ctx: ToolContext, query: str, kind: str, limit: int = 8, orientation: str | None = None,
                       min_duration: float | None = None, max_duration: float | None = None, instrumental: bool = False,
                       allow_noncommercial: bool = False, sources: list[str] | None = None) -> dict[str, Any]:
    cfg = _cfg(ctx)
    commercial = not allow_noncommercial and cfg.get("commercial_only", True) is not False
    try:
        items, warnings = await stock.search(cfg, kind, query, limit=max(1, min(16, int(limit or 8))), orientation=orientation,
                                             min_duration=min_duration, max_duration=max_duration, commercial=commercial,
                                             instrumental=instrumental, sources=sources)
    except stock.StockError as e:
        raise ToolError(str(e)) from e
    rows = []
    for i, it in enumerate(items, 1):
        row: dict[str, Any] = {"n": i, "id": it["id"], "title": (it.get("title") or "")[:70], "source": it["source"],
                               "license": it["license"] + ("（需署名）" if it["attribution"] else "")}
        if it.get("duration"):
            row["duration"] = round(float(it["duration"]), 1)
        if it.get("width") and it.get("height"):
            row["size"] = f"{it['width']}x{it['height']}"
        if it.get("tags"):
            row["tags"] = ", ".join(str(t) for t in it["tags"][:6])
        if it.get("author"):
            row["author"] = it["author"][:40]
        rows.append(row)
    out: dict[str, Any] = {"kind": kind, "query": query, "results": rows}
    if warnings:
        out["warnings"] = warnings
    if not items:
        out["hint"] = "没有找到合适的结果：换几个英文同义词、放宽时长/方向限制再试。"
    keyed = [p for p in stock.ORDER[kind] if stock.PROVIDERS[p]["key"]]
    if not any(p in stock.available(cfg, kind) for p in keyed):
        names = " / ".join(stock.PROVIDERS[p]["label"] for p in keyed)
        out["tip"] = f"目前只用了免 key 的素材库；在 设置 → 素材 免费申请 {names} 的 key 后，{stock.KIND_LABEL[kind]}会多很多、质量更高。"
    if kind in ("video", "image") and items:
        sheet = await _sheet(ctx, items, cfg)
        if sheet:
            out["images"] = [str(sheet)]
    return out


def _slug(t: str) -> str:
    return re.sub(r"[^\w一-鿿]+", "-", (t or "").lower()).strip("-_")[:40] or "asset"


async def _fetch(cfg: dict[str, Any], url: str, stem: Path, ext: str, report: Any) -> Path:
    async with stock.client(cfg, timeout=60) as c, c.stream("GET", url) as r:
        if r.status_code >= 400:
            raise ToolError(f"下载失败（HTTP {r.status_code}）")
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype.startswith("text/html"):
            raise ToolError("下载地址返回的是网页而不是文件，换一个素材试试")
        ext = CTYPE_EXT.get(ctype, ext)
        total = int(r.headers.get("content-length") or 0)
        dest = stem.with_name(f"{stem.name}.{ext}")
        part = dest.with_suffix(dest.suffix + ".part")
        got = 0
        with part.open("wb") as f:
            async for chunk in r.aiter_bytes(256 * 1024):
                f.write(chunk)
                got += len(chunk)
                if got > MAX_BYTES:
                    raise ToolError("文件太大（超过 800MB）")
                if total:
                    await report(got / total)
        part.replace(dest)
        return dest


async def fetch_item(ctx: ToolContext, cfg: dict[str, Any], it: dict[str, Any], max_side: int = 1080,
                     report: Any = None) -> Path:
    """Download one search result into assets/<kind>/ (reusing an earlier download); ogv/ogg video → mp4."""
    async def _quiet(_f: float) -> None:
        return None

    f = stock.pick_file(it, max_side)
    native = it["id"].split(":", 1)[1]
    folder = ctx.workspace / ASSET_DIRS[it["kind"]]
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"{it['provider']}-{native[:16]}-{_slug(it.get('title', ''))}"
    existing = [p for p in folder.glob(stem.name + ".*") if not p.name.endswith(".part")]
    path = existing[0] if existing else await _fetch(cfg, f["url"], stem, (f.get("ext") or "bin").lower(), report or _quiet)
    if it["kind"] == "video" and path.suffix.lower() in (".ogv", ".ogg"):
        mp4 = path.with_suffix(".mp4")   # Theora won't preview in browsers; H.264 plays everywhere
        await ff.ffmpeg(["-i", str(path), *ff.ENC_V, "-c:a", "aac", "-b:a", "160k", *ff.MP4_FLAGS, str(mp4)])
        path.unlink(missing_ok=True)
        path = mp4
    return path


def add_credit(ctx: ToolContext, it: dict[str, Any], rel: str) -> str:
    line = stock.credit_line(it, rel)
    f = ctx.workspace / CREDITS
    old = f.read_text("utf-8") if f.exists() else "# 素材来源与授权\n\n由 AutoStudio 下载素材时自动记录。标注「需署名」的素材，发布作品时请在简介中注明作者和授权。\n\n"
    tag = f"<!-- {it['id']} -->"
    if tag not in old:
        need = "（需署名）" if it["attribution"] else ""
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(old + f"- `{rel}` — {line}{need} {tag}\n", "utf-8")
    return line


@tool("asset_download",
      "下载 asset_search 找到的素材到工作区 assets/（videos / images / music / sfx），并把来源和授权记到 assets/CREDITS.md。"
      "返回本地路径、时长；音乐会附带 BPM，方便卡点。",
      obj(["ids"], ids=arr(s("asset_search 结果里的 id"), "要下载的素材 id，可多个"),
          quality=s("视频/图片清晰度，默认 1080p", enum=["720p", "1080p", "4k"])),
      category="assets")
async def asset_download(ctx: ToolContext, ids: list[str] | str, quality: str = "1080p") -> dict[str, Any]:
    cfg = _cfg(ctx)
    ids = [ids] if isinstance(ids, str) else list(ids or [])
    if not ids:
        raise ToolError("请提供要下载的素材 id")
    max_side = {"720p": 720, "1080p": 1080, "4k": 4000}.get(quality, 1080)
    done: list[dict[str, Any]] = []
    files: list[str] = []
    for n, cid in enumerate(ids):
        it = stock.lookup(str(cid))
        if not it:
            done.append({"id": cid, "ok": False, "error": "找不到这个 id：请先用 asset_search 搜索（AutoStudio 重启后需要重新搜索）"})
            continue

        async def report(frac: float, n: int = n, title: str = it.get("title", "")) -> None:
            await ctx.progress((n + frac) / len(ids), f"下载 {title[:30]}")

        try:
            path = await fetch_item(ctx, cfg, it, max_side, report)
        except (ToolError, httpx.HTTPError, ff.FFmpegError) as e:
            done.append({"id": cid, "ok": False, "error": str(e)[:200]})
            continue
        rel = ctx.rel(path)
        entry: dict[str, Any] = {"id": cid, "ok": True, "path": rel, "title": it.get("title"), "license": it["license"],
                                 "attribution_required": it["attribution"], "credit": add_credit(ctx, it, rel)}
        try:
            info = await ff.probe(path)
            if info.get("duration"):
                entry["duration"] = round(float(info["duration"]), 2)
            if info.get("width") and it["kind"] in ("video", "image"):
                entry["size"] = f"{info['width']}x{info['height']}"
        except Exception:  # noqa: BLE001 - metadata is a bonus
            pass
        if it["kind"] == "music":
            try:
                from ..media.beats import analyze, load_mono

                res = await asyncio.to_thread(analyze, await load_mono(path, max_seconds=90))
                if res.get("bpm"):
                    entry["bpm"] = round(float(res["bpm"]), 1)
            except Exception:  # noqa: BLE001
                pass
        files.append(str(path))
        done.append(entry)
    ok = [d for d in done if d.get("ok")]
    out: dict[str, Any] = {"ok": bool(ok), "downloaded": done, "files": files, "credits_file": CREDITS}
    if any(d.get("attribution_required") for d in ok):
        out["note"] = "部分素材需要署名：发布时把对应的 credit 写进视频简介（完整列表在 assets/CREDITS.md）。"
    return out
