"""Royalty-free asset search: stock footage, images, music (BGM) and sound effects.

Works out of the box with keyless sources (Openverse: Jamendo music + Freesound effects + CC images;
Wikimedia Commons: footage and images). Free API keys unlock better catalogs: Pexels and Pixabay
(stock video + photos), Freesound (effects), Jamendo (music). Every result carries its license so the
agent can keep to commercially usable material and credit authors when a license asks for it.
"""
from __future__ import annotations

import asyncio
import html
import re
import urllib.parse
import urllib.request
from typing import Any

import httpx

UA = "AutoStudio/0.1 (+https://github.com/jindinghe123-tech/autocliper)"
KINDS = ("video", "image", "music", "sfx")
KIND_LABEL = {"video": "视频画面", "image": "图片", "music": "音乐", "sfx": "音效"}

PROVIDERS: dict[str, dict[str, Any]] = {
    "pexels": {"label": "Pexels", "kinds": ["video", "image"], "key": "pexels_key",
               "signup": "https://www.pexels.com/api/new/", "desc": "高质量实拍视频和照片，可免费商用、无需署名"},
    "pixabay": {"label": "Pixabay", "kinds": ["video", "image"], "key": "pixabay_key",
                "signup": "https://pixabay.com/api/docs/", "desc": "实拍视频、照片、插画，可免费商用、无需署名"},
    "freesound": {"label": "Freesound", "kinds": ["sfx"], "key": "freesound_key",
                  "signup": "https://freesound.org/apiv2/apply", "desc": "60 万+ 音效（转场、点击、环境声…）"},
    "jamendo": {"label": "Jamendo", "kinds": ["music"], "key": "jamendo_client_id",
                "signup": "https://devportal.jamendo.com/", "desc": "60 万+ 首独立音乐，可按曲风/情绪/纯音乐搜索"},
    "openverse": {"label": "Openverse", "kinds": ["image", "music", "sfx"], "key": None,
                  "signup": "https://openverse.org", "desc": "免 key：CC 授权图片、Jamendo 音乐、Freesound 音效（匿名每天约 200 次）"},
    "wikimedia": {"label": "Wikimedia Commons", "kinds": ["video", "image"], "key": None,
                  "signup": "https://commons.wikimedia.org", "desc": "免 key：自由授权的视频和图片"},
}
ORDER = {"video": ["pexels", "pixabay", "wikimedia"], "image": ["pexels", "pixabay", "openverse", "wikimedia"],
         "music": ["jamendo", "openverse"], "sfx": ["freesound", "openverse"]}

_CACHE: dict[str, dict[str, Any]] = {}   # candidate id -> full record (download urls etc.)


class StockError(RuntimeError):
    pass


def remember(items: list[dict[str, Any]]) -> None:
    for it in items:
        _CACHE[it["id"]] = it
    while len(_CACHE) > 800:
        _CACHE.pop(next(iter(_CACHE)))


def lookup(cid: str) -> dict[str, Any] | None:
    return _CACHE.get(cid)


def proxy_for(cfg: dict[str, Any]) -> str | None:
    """Explicit setting, else the environment / Windows system proxy (Clash, v2rayN…)."""
    if cfg.get("proxy"):
        return cfg["proxy"]
    try:
        return urllib.request.getproxies().get("https") or urllib.request.getproxies().get("http")
    except Exception:  # noqa: BLE001
        return None


def client(cfg: dict[str, Any], timeout: float = 25) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA}, proxy=proxy_for(cfg))


def available(cfg: dict[str, Any], kind: str) -> list[str]:
    return [p for p in ORDER[kind] if not PROVIDERS[p]["key"] or (cfg.get(PROVIDERS[p]["key"]) or "").strip()]


# ---------------------------------------------------------------------------
# licenses

def cc_info(code: str, version: str = "") -> dict[str, Any]:
    """Normalize a Creative Commons style code ('by-nc-sa', 'cc0', 'pdm', a CC URL…)."""
    c = (code or "").lower().strip()
    if "creativecommons.org" in c:
        m = re.search(r"/(?:licenses|publicdomain)/([a-z+-]+)/?([\d.]*)", c)
        c, version = (m.group(1), m.group(2)) if m else (c, version)
        c = {"zero": "cc0", "mark": "pdm"}.get(c, c)
    if c in ("cc0", "pdm", "public domain", "pd") or "public domain" in c:
        return {"license": "CC0 / 公有领域" if c != "pdm" else "公有领域", "commercial": True, "modify": True, "attribution": False}
    name = "CC " + c.upper().replace("CC-", "").replace("CC ", "") + (f" {version}" if version else "")
    return {"license": name.strip(), "commercial": "nc" not in c, "modify": "nd" not in c, "attribution": True}


def _usable(item: dict[str, Any], commercial: bool) -> bool:
    return item["modify"] and (item["commercial"] or not commercial)


def _orient_ok(w: int | None, h: int | None, orientation: str | None) -> bool:
    if not orientation or not w or not h:
        return True
    r = w / h
    return {"landscape": r > 1.1, "portrait": r < 0.9, "square": 0.9 <= r <= 1.1}.get(orientation, True)


def _dur_ok(d: float | None, lo: float | None, hi: float | None) -> bool:
    return d is None or ((lo is None or d >= lo) and (hi is None or d <= hi))


def _big_enough(it: dict[str, Any], kind: str) -> bool:
    """Skip thumbnails-sized media that would look blurry in a video."""
    w, h = it.get("width"), it.get("height")
    return kind not in ("video", "image") or not w or not h or min(w, h) >= (480 if kind == "video" else 600)


def url_ext(url: str, default: str = "bin") -> str:
    tail = urllib.parse.urlparse(url or "").path.rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[-1].lower() if "." in tail else default


def _cjk(s: str) -> bool:
    return bool(re.search(r"[一-鿿]", s))


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


# ---------------------------------------------------------------------------
# providers — each returns normalized candidates

async def _pexels(c: httpx.AsyncClient, cfg: dict[str, Any], kind: str, q: str, o: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"query": q, "per_page": min(40, o["limit"] * 2)}
    if o.get("orientation"):
        params["orientation"] = o["orientation"]
    if _cjk(q):
        params["locale"] = "zh-CN"
    url = "https://api.pexels.com/videos/search" if kind == "video" else "https://api.pexels.com/v1/search"
    r = await c.get(url, params=params, headers={"Authorization": cfg["pexels_key"].strip()})
    if r.status_code in (401, 403):
        raise StockError("API key 无效")
    r.raise_for_status()
    lic = {"license": "Pexels License", "commercial": True, "modify": True, "attribution": False}
    out = []
    if kind == "video":
        for v in r.json().get("videos", []):
            files = [{"url": f["link"], "width": f.get("width"), "height": f.get("height"), "ext": "mp4"}
                     for f in v.get("video_files", []) if f.get("link") and (f.get("file_type") or "").endswith("mp4")]
            slug = (v.get("url") or "").rstrip("/").split("/")[-1]
            out.append({"id": f"pexels:v{v['id']}", "title": re.sub(r"-\d+$", "", slug).replace("-", " ") or q,
                        "duration": v.get("duration"), "width": v.get("width"), "height": v.get("height"),
                        "thumb": v.get("image"), "files": files, "author": (v.get("user") or {}).get("name", ""),
                        "page": v.get("url"), **lic})
    else:
        for p in r.json().get("photos", []):
            src = p.get("src") or {}
            out.append({"id": f"pexels:p{p['id']}", "title": p.get("alt") or q, "width": p.get("width"), "height": p.get("height"),
                        "thumb": src.get("medium") or src.get("small"),
                        "files": [{"url": src.get("large2x") or src.get("original"), "ext": "jpg"},
                                  {"url": src.get("original"), "ext": "jpg", "max": True}],
                        "author": p.get("photographer", ""), "page": p.get("url"), **lic})
    return out


async def _pixabay(c: httpx.AsyncClient, cfg: dict[str, Any], kind: str, q: str, o: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"key": cfg["pixabay_key"].strip(), "q": q[:100], "per_page": max(3, min(50, o["limit"] * 2)),
                              "safesearch": "true", "lang": "zh" if _cjk(q) else "en"}
    lic = {"license": "Pixabay Content License", "commercial": True, "modify": True, "attribution": False}
    out = []
    if kind == "video":
        r = await c.get("https://pixabay.com/api/videos/", params=params)
        if r.status_code in (400, 401) and "key" in r.text.lower():
            raise StockError("API key 无效")
        r.raise_for_status()
        for v in r.json().get("hits", []):
            vids = v.get("videos") or {}
            files = [{"url": f["url"], "width": f.get("width"), "height": f.get("height"), "ext": "mp4"}
                     for f in (vids.get(k) or {} for k in ("large", "medium", "small", "tiny")) if f.get("url")]
            big = files[0] if files else {}
            thumb = next((f.get("thumbnail") for f in (vids.get(k) or {} for k in ("medium", "small", "large")) if f.get("thumbnail")), None)
            out.append({"id": f"pixabay:v{v['id']}", "title": v.get("tags") or q, "duration": v.get("duration"),
                        "width": big.get("width"), "height": big.get("height"), "thumb": thumb, "files": files,
                        "author": v.get("user", ""), "page": v.get("pageURL"), **lic})
    else:
        params.update(image_type="photo")
        if o.get("orientation") in ("landscape", "portrait"):
            params["orientation"] = "horizontal" if o["orientation"] == "landscape" else "vertical"
        r = await c.get("https://pixabay.com/api/", params=params)
        if r.status_code in (400, 401) and "key" in r.text.lower():
            raise StockError("API key 无效")
        r.raise_for_status()
        for p in r.json().get("hits", []):
            out.append({"id": f"pixabay:p{p['id']}", "title": p.get("tags") or q, "width": p.get("imageWidth"),
                        "height": p.get("imageHeight"), "thumb": p.get("webformatURL") or p.get("previewURL"),
                        "files": [{"url": p.get("largeImageURL") or p.get("webformatURL"), "ext": "jpg"}],
                        "author": p.get("user", ""), "page": p.get("pageURL"), **lic})
    return out


async def _freesound(c: httpx.AsyncClient, cfg: dict[str, Any], kind: str, q: str, o: dict[str, Any]) -> list[dict[str, Any]]:
    lo, hi = o.get("min_duration") or 0, o.get("max_duration") or 30
    flt = f"duration:[{lo} TO {hi}]"
    if o.get("commercial", True):
        flt += ' license:("Attribution" OR "Creative Commons 0")'
    r = await c.get("https://freesound.org/apiv2/search/text/", params={
        "query": q, "token": cfg["freesound_key"].strip(), "page_size": min(40, o["limit"] * 2), "filter": flt,
        "fields": "id,name,tags,duration,license,username,previews,url", "sort": "score"})
    if r.status_code in (401, 403):
        raise StockError("API key 无效")
    r.raise_for_status()
    out = []
    for s in r.json().get("results", []):
        pv = s.get("previews") or {}
        out.append({"id": f"freesound:{s['id']}", "title": s.get("name") or q, "duration": s.get("duration"),
                    "tags": (s.get("tags") or [])[:6], "files": [{"url": pv.get("preview-hq-mp3") or pv.get("preview-lq-mp3"), "ext": "mp3"}],
                    "author": s.get("username", ""), "page": s.get("url"), **cc_info(s.get("license", ""))})
    return out


async def _jamendo(c: httpx.AsyncClient, cfg: dict[str, Any], kind: str, q: str, o: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"client_id": cfg["jamendo_client_id"].strip(), "format": "json", "limit": min(50, o["limit"] * 3),
                              "search": q, "include": "musicinfo", "audioformat": "mp32", "boost": "popularity_total"}
    if o.get("instrumental"):
        params["vocalinstrumental"] = "instrumental"
    r = await c.get("https://api.jamendo.com/v3.0/tracks/", params=params)
    r.raise_for_status()
    d = r.json()
    if (d.get("headers") or {}).get("status") == "failed":
        raise StockError((d["headers"].get("error_message") or "请求失败")[:120])
    out = []
    for t in d.get("results", []):
        url = t.get("audiodownload") if t.get("audiodownload_allowed") else None
        if not url:
            continue
        tags = (t.get("musicinfo") or {}).get("tags") or {}
        out.append({"id": f"jamendo:{t['id']}", "title": t.get("name") or q, "duration": t.get("duration"),
                    "tags": (tags.get("genres") or [])[:3] + (tags.get("vartags") or [])[:3],
                    "files": [{"url": url, "ext": "mp3"}], "thumb": t.get("image"),
                    "author": t.get("artist_name", ""), "page": t.get("shareurl"), **cc_info(t.get("license_ccurl", ""))})
    return out


async def _openverse(c: httpx.AsyncClient, cfg: dict[str, Any], kind: str, q: str, o: dict[str, Any]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"q": q, "page_size": min(20, max(o["limit"] * 2, 6))}
    if o.get("commercial", True):
        params["license_type"] = "commercial,modification"
    else:
        params["license_type"] = "modification"
    if kind == "image":
        if o.get("orientation"):
            params["aspect_ratio"] = {"landscape": "wide", "portrait": "tall", "square": "square"}[o["orientation"]]
        params["size"] = "large"
        url = "https://api.openverse.org/v1/images/"
    else:
        params["source"] = "jamendo" if kind == "music" else "freesound"
        url = "https://api.openverse.org/v1/audio/"
    r = await c.get(url, params=params)
    if r.status_code == 429:
        raise StockError("匿名访问次数用完了（每分钟 20 次 / 每天 200 次），稍后再试或在设置里填其它素材库的 key")
    r.raise_for_status()
    out = []
    for it in r.json().get("results", []):
        lic = cc_info(it.get("license", ""), it.get("license_version") or "")
        rec = {"id": f"openverse:{it['id']}", "title": it.get("title") or q, "author": it.get("creator") or "",
               "page": it.get("foreign_landing_url"), "source_site": it.get("source"), **lic,
               "tags": [t.get("name") for t in (it.get("tags") or [])[:6] if t.get("name")]}
        if kind == "image":
            rec.update(width=it.get("width"), height=it.get("height"), thumb=it.get("thumbnail"),
                       files=[{"url": it.get("url"), "ext": (it.get("filetype") or "jpg")}])
        else:
            ext = {"mp32": "mp3", "mp31": "mp3"}.get(it.get("filetype") or "", it.get("filetype") or "mp3")
            rec.update(duration=(it.get("duration") or 0) / 1000 or None, files=[{"url": it.get("url"), "ext": ext}],
                       tags=(it.get("genres") or [])[:4] or rec["tags"])
        out.append(rec)
    return out


async def _wikimedia(c: httpx.AsyncClient, cfg: dict[str, Any], kind: str, q: str, o: dict[str, Any]) -> list[dict[str, Any]]:
    r = await c.get("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": min(30, o["limit"] * 3),
        "gsrsearch": f"{q} filetype:{'video' if kind == 'video' else 'bitmap'}", "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 640,
        "iiextmetadatafilter": "LicenseShortName|Artist|ObjectName"})
    r.raise_for_status()
    pages = sorted(((r.json().get("query") or {}).get("pages") or {}).values(), key=lambda p: p.get("index", 0))
    out = []
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        lic_name = (meta.get("LicenseShortName") or {}).get("value", "")
        low = lic_name.lower()
        if not lic_name or "fair use" in low:
            continue
        free = "public domain" in low or low.startswith(("cc0", "pd"))
        lic = {"license": lic_name, "commercial": "nc" not in low.replace("-", " ").split() and "-nc" not in low,
               "modify": "nd" not in low.replace("-", " ").split() and "-nd" not in low, "attribution": not free}
        w, h = ii.get("width"), ii.get("height")
        url, thumb = ii.get("url"), ii.get("thumburl")
        files = [{"url": url, "width": w, "height": h, "ext": url_ext(url)}]
        if kind == "image" and thumb and (w or 0) > 3000:
            files.insert(0, {"url": thumb.replace("/640px-", "/1920px-"), "ext": "jpg"})
        title = (meta.get("ObjectName") or {}).get("value") or p.get("title", "").removeprefix("File:").rsplit(".", 1)[0]
        out.append({"id": f"wikimedia:{p.get('pageid')}", "title": _strip_html(title)[:80], "width": w, "height": h,
                    "duration": ii.get("duration"), "thumb": thumb, "files": files,
                    "author": _strip_html((meta.get("Artist") or {}).get("value", ""))[:60],
                    "page": ii.get("descriptionurl"), **lic})
    return out


SEARCH = {"pexels": _pexels, "pixabay": _pixabay, "freesound": _freesound, "jamendo": _jamendo,
          "openverse": _openverse, "wikimedia": _wikimedia}


async def search(cfg: dict[str, Any], kind: str, query: str, *, limit: int = 8, orientation: str | None = None,
                 min_duration: float | None = None, max_duration: float | None = None, commercial: bool = True,
                 instrumental: bool = False, sources: list[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Search every usable provider for `kind`, filter, interleave → (candidates, warnings)."""
    if kind not in KINDS:
        raise StockError(f"kind 只能是 {'/'.join(KINDS)}")
    provs = [p for p in available(cfg, kind) if not sources or p in sources]
    if not provs:
        raise StockError("没有可用的素材库" + (f"：{', '.join(sources)} 需要先在 设置 → 素材 里填 API key" if sources else ""))
    opts = {"limit": limit, "orientation": orientation, "min_duration": min_duration, "max_duration": max_duration,
            "commercial": commercial, "instrumental": instrumental}
    warnings: list[str] = []
    async with client(cfg) as c:
        results = await asyncio.gather(*(SEARCH[p](c, cfg, kind, query, opts) for p in provs), return_exceptions=True)
    lists: list[list[dict[str, Any]]] = []
    for p, res in zip(provs, results):
        label = PROVIDERS[p]["label"]
        if isinstance(res, BaseException):
            msg = str(res) if isinstance(res, StockError) else f"{type(res).__name__}: {str(res)[:120]}"
            warnings.append(f"{label}：{msg}")
            continue
        good = [dict(it, provider=p, source=label, kind=kind) for it in res
                if it.get("files") and it["files"][0].get("url") and _usable(it, commercial) and _big_enough(it, kind)
                and _orient_ok(it.get("width"), it.get("height"), orientation)
                and _dur_ok(it.get("duration"), min_duration, max_duration)]
        lists.append(good)
    merged: list[dict[str, Any]] = []
    for i in range(max((len(x) for x in lists), default=0)):
        merged += [x[i] for x in lists if i < len(x)]
    merged = merged[:limit]
    remember(merged)
    return merged, warnings


def pick_file(item: dict[str, Any], max_side: int = 1080) -> dict[str, Any]:
    """The rendition whose short side is closest to (but not above) `max_side`, else the smallest bigger one."""
    files = [f for f in item["files"] if f.get("url")]
    if max_side >= 4000:
        return next((f for f in files if f.get("max")), None) or max(files, key=lambda f: (f.get("width") or 0) * (f.get("height") or 0))
    sized = [f for f in files if f.get("width") and f.get("height")]
    if not sized:
        return next((f for f in files if not f.get("max")), files[0])
    fit = [f for f in sized if min(f["width"], f["height"]) <= max_side]
    if fit:
        return max(fit, key=lambda f: f["width"] * f["height"])
    return min(sized, key=lambda f: f["width"] * f["height"])


def credit_line(item: dict[str, Any], rel_path: str) -> str:
    by = f" by {item['author']}" if item.get("author") else ""
    site = item.get("source_site")
    src = f"{item['source']}（{site.replace('_', ' ').title()}）" if site else item["source"]
    return f"《{item.get('title') or rel_path}》{by} · {item['license']} · {src} {item.get('page') or ''}".strip()
