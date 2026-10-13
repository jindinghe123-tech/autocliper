"""Topic research: live trending lists from Chinese platforms (no login needed)."""
from __future__ import annotations

import asyncio
import html
import re
import time
import urllib.parse
from typing import Any

import httpx

from .registry import ToolContext, arr, integer, obj, s, tool

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
# Topics a light-hearted short video should normally stay away from: politics, war, death, crime, disasters.
RISKY = re.compile(r"习近平|总书记|国家主席|总理|外交部|国务院|总统|首相|政府|台海|台独|战争|开战|军演|导弹|身亡|死亡|遇难|丧生|"
                   r"去世|离世|自杀|杀害|遇害|命案|案件|嫌疑人|被捕|判处|宣判|判决|事故|爆炸|火灾|地震|洪水|坠机|疫情|恐袭|"
                   r"性侵|猥亵|家暴|癌症?晚期|确诊|轻生|跳河|跳楼|网暴")
RISKY_CATS = {"international", "taiwan", "military", "politics", "society", "accident"}


def _flag(title: str, cats: list[str] | None = None) -> str | None:
    m = RISKY.search(title or "")
    if m:
        return f"含「{m.group(0)}」"
    bad = sorted(set(cats or []) & RISKY_CATS)
    return f"分类：{bad[0]}" if bad else None


async def _baidu(c: httpx.AsyncClient, limit: int) -> list[dict[str, Any]]:
    r = await c.get("https://top.baidu.com/api/board", params={"platform": "wise", "tab": "realtime"})
    r.raise_for_status()
    card = (r.json().get("data") or {}).get("cards", [{}])[0]
    items = card.get("content") or []
    if items and isinstance(items[0], dict) and "content" in items[0]:
        items = items[0]["content"]
    out = []
    for it in items:
        if it.get("isTop"):
            continue   # pinned official item, not a trending topic
        out.append({"title": it.get("word") or it.get("query"), "summary": it.get("desc") or "",
                    "heat": it.get("hotScore"), "url": it.get("url")})
    return out[:limit]


async def _zhihu(c: httpx.AsyncClient, limit: int) -> list[dict[str, Any]]:
    r = await c.get("https://api.zhihu.com/topstory/hot-lists/total", params={"limit": max(limit, 10)})
    r.raise_for_status()
    out = []
    for it in r.json().get("data", []):
        t = it.get("target") or {}
        qid = t.get("id")
        out.append({"title": t.get("title") or (t.get("title_area") or {}).get("text"),
                    "summary": (t.get("excerpt") or (t.get("excerpt_area") or {}).get("text") or "")[:120],
                    "heat": it.get("detail_text") or (t.get("metrics_area") or {}).get("text"),
                    "url": f"https://www.zhihu.com/question/{qid}" if qid else t.get("url")})
    return out[:limit]


async def _toutiao(c: httpx.AsyncClient, limit: int) -> list[dict[str, Any]]:
    r = await c.get("https://www.toutiao.com/hot-event/hot-board/", params={"origin": "toutiao_pc"})
    r.raise_for_status()
    return [{"title": it.get("Title"), "heat": it.get("HotValue"), "url": (it.get("Url") or "").split("?")[0],
             "cats": it.get("InterestCategory") or []} for it in r.json().get("data", [])][:limit]


async def _bilibili(c: httpx.AsyncClient, limit: int) -> list[dict[str, Any]]:
    r = await c.get("https://s.search.bilibili.com/main/hotword")
    r.raise_for_status()
    return [{"title": it.get("show_name") or it.get("keyword"), "heat": it.get("heat_score"),
             "url": f"https://search.bilibili.com/all?keyword={it.get('keyword')}"} for it in r.json().get("list", [])][:limit]


SOURCES = {"baidu": ("百度热搜", _baidu), "zhihu": ("知乎热榜", _zhihu), "toutiao": ("今日头条", _toutiao),
           "bilibili": ("B站热搜", _bilibili)}


@tool("trending_topics",
      "获取实时热搜榜做选题：百度热搜、知乎热榜、今日头条、B站热搜（不需要登录）。每条带标题、热度、简介和链接；"
      "risky 标出了时政 / 灾难事故 / 刑事案件 / 疾病死亡类话题，做轻松的短视频时应避开。"
      "选定话题后用 web_fetch 打开链接或搜索，核实事实再写稿。",
      obj(sources=arr(s("热榜", enum=list(SOURCES)), "要看哪些热榜，默认全部"),
          limit=integer("每个榜单返回条数，默认 20")), category="web")
async def trending_topics(ctx: ToolContext, sources: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
    picked = [k for k in (sources or list(SOURCES)) if k in SOURCES]
    limit = max(5, min(50, int(limit or 20)))
    async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                 headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}) as c:
        results = await asyncio.gather(*(SOURCES[k][1](c, limit) for k in picked), return_exceptions=True)
    boards: dict[str, list[dict[str, Any]]] = {}
    errors = []
    for key, res in zip(picked, results):
        label = SOURCES[key][0]
        if isinstance(res, BaseException):
            errors.append(f"{label}：{type(res).__name__} {str(res)[:80]}")
            continue
        rows = []
        for i, it in enumerate(res, 1):
            if not it.get("title"):
                continue
            row = {"rank": i, "title": it["title"]}
            for k in ("heat", "summary", "url"):
                if it.get(k):
                    row[k] = it[k]
            risk = _flag(it["title"] + " " + (it.get("summary") or ""), it.get("cats"))
            if risk:
                row["risky"] = risk
            rows.append(row)
        boards[label] = rows
    out: dict[str, Any] = {"fetched_at": time.strftime("%Y-%m-%d %H:%M"), "boards": boards}
    if errors:
        out["errors"] = errors
    out["tip"] = ("选题：优先选多个榜单都在的、有画面感、能讲出信息增量的话题（科技、生活技巧、文化、奇闻、消费、体育娱乐的轻松向）；"
                  "避开 risky 标记的话题和无法核实的传闻。")
    return out


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


async def _ddg(c: httpx.AsyncClient, q: str) -> list[dict[str, str]]:
    r = await c.post("https://html.duckduckgo.com/html/", data={"q": q, "kl": "cn-zh" if re.search(r"[\u4e00-\u9fff]", q) else ""})
    r.raise_for_status()
    out = []
    for block in re.split(r'<div class="result results_links', r.text)[1:]:
        a = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        href = html.unescape(a.group(1))
        m = re.search(r"uddg=([^&]+)", href)
        url = urllib.parse.unquote(m.group(1)) if m else href
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        out.append({"title": _clean(a.group(2)), "url": url, "snippet": _clean(snip.group(1)) if snip else ""})
    return out


async def _bing(c: httpx.AsyncClient, q: str) -> list[dict[str, str]]:
    r = await c.get("https://www.bing.com/search", params={"q": q, "setlang": "zh-CN", "ensearch": "0"})
    r.raise_for_status()
    out = []
    for block in re.split(r'<li class="b_algo"', r.text)[1:]:
        a = re.search(r"<h2[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, re.S)
        if not a:
            continue
        snip = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        out.append({"title": _clean(a.group(2)), "url": html.unescape(a.group(1)), "snippet": _clean(snip.group(1)) if snip else ""})
    return out


@tool("web_search", "搜索网页（DuckDuckGo，失败时用必应），返回标题、链接和摘要。核实事实、找资料来源时用；"
      "需要细节再用 web_fetch 打开结果链接。关键词要简短，2~4 个词效果最好。",
      obj(["query"], query=s("搜索关键词"), limit=integer("结果数，默认 8")), category="web")
async def web_search(ctx: ToolContext, query: str, limit: int = 8) -> dict[str, Any]:
    limit = max(1, min(20, int(limit or 8)))
    errors = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}) as c:
        for name, fn in (("DuckDuckGo", _ddg), ("Bing", _bing)):
            try:
                res = await fn(c, query)
            except Exception as e:  # noqa: BLE001 - try the next engine
                errors.append(f"{name}: {type(e).__name__}")
                continue
            if res:
                return {"engine": name, "query": query, "results": res[:limit]}
            errors.append(f"{name}: 没有结果")
    return {"query": query, "results": [], "errors": errors, "hint": "换更短、更常见的关键词再试"}
