"""Social publishing tools: list accounts, publish to one or many accounts, check publish history."""
from __future__ import annotations

from typing import Any

from .. import social, state
from .registry import ToolContext, any_of_obj, arr, boolean, integer, obj, s, tool

STATUS_LABEL = {"pending": "待确认", "publishing": "发布中", "published": "已发布", "failed": "失败", "cancelled": "已取消"}


@tool("social_accounts", "列出用户已添加的社交媒体账号（小红书、YouTube，每个平台可以有多个账号）及登录状态。发布前先用它确认账号。",
      obj(), category="social")
async def social_accounts(ctx: ToolContext) -> dict[str, Any]:
    accs = [social.account_view(a) for a in state.db.list_accounts()]
    if not accs:
        return {"accounts": [], "hint": "还没有添加社交账号。请让用户打开左侧「发布」页面，添加小红书或 YouTube 账号。"}
    return {"accounts": [{"id": a["id"], "platform": a["platform_label"], "name": a["name"], "handle": a["handle"],
                          "status": "正常" if a["status"] == "ok" else "登录已失效，需要用户在发布页重新登录"} for a in accs]}


def _paths(ctx: ToolContext, raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    for k in ("video", "cover"):
        if out.get(k):
            out[k] = str(ctx.resolve(out[k], must_exist=True))
    if out.get("images"):
        out["images"] = [str(ctx.resolve(i, must_exist=True)) for i in out["images"]]
    return out


@tool("social_publish",
      "把视频或图文发布到用户已登录的社交账号，可一次发到多个账号/平台。"
      "小红书：视频笔记，或 1~18 张图片的图文笔记；标题 ≤20 字，正文 ≤1000 字，话题会自动变成 #话题。"
      "YouTube：只能发视频；标题 ≤100 字，简介 ≤5000 字；竖屏且 ≤3 分钟的视频会自动成为 Shorts。"
      "accounts 填账号名称或 id；填平台名（\"小红书\" / \"youtube\"）表示该平台的全部账号，填 \"全部\" 表示所有账号。"
      "各平台文案要求不同，可用 per_platform 分别指定（例如小红书用更短更口语的标题、更多话题）。"
      "只在成片已经确认无误时调用；用户只说“准备好/先别发”时设置 queue_only=true。",
      obj(["accounts"],
          accounts=arr(s("账号名称 / id / 平台名 / 全部"), "发布到哪些账号"),
          video=s("视频文件路径"),
          images=arr(s("图片路径"), "小红书图文笔记的图片，1~18 张（与 video 二选一）"),
          title=s("标题"),
          description=s("正文 / 简介，可以换行"),
          tags=arr(s("话题，不带 #"), "话题标签"),
          cover=s("封面图路径（可选）"),
          youtube_privacy=s("YouTube 可见性，默认 public", enum=["public", "unlisted", "private"]),
          per_platform=any_of_obj('按平台覆盖上面的字段，例如 {"小红书": {"title": "…", "tags": ["…"]}, "youtube": {"title": "…"}}'),
          queue_only=boolean("只放进「待确认」队列，等用户在发布页点确认后再发")),
      category="social", dangerous=True)
async def social_publish(ctx: ToolContext, accounts: list[str] | str, video: str | None = None, images: list[str] | None = None,
                         title: str = "", description: str = "", tags: list[str] | None = None, cover: str | None = None,
                         youtube_privacy: str | None = None, per_platform: dict[str, Any] | None = None,
                         queue_only: bool = False) -> dict[str, Any]:
    targets = social.resolve_accounts(accounts if isinstance(accounts, list) else [accounts])
    base = _paths(ctx, {"video": video, "images": images or [], "title": title, "description": description,
                        "tags": tags or [], "cover": cover, "privacy": youtube_privacy})
    overrides: dict[str, dict[str, Any]] = {}
    for k, v in (per_platform or {}).items():
        plat = social.ALIASES.get(str(k).strip().lower())
        if plat and isinstance(v, dict):
            v = {("privacy" if kk == "youtube_privacy" else kk): vv for kk, vv in v.items()}
            overrides[plat] = _paths(ctx, v)

    # validate everything before publishing anything
    prepared = []
    for acc in targets:
        warnings: list[str] = []
        content = social.prepare(acc["platform"], {**base, **overrides.get(acc["platform"], {})}, warnings)
        prepared.append((acc, content, warnings))

    mode = "queue" if queue_only else ctx.publish_mode
    session = state.db.get_session(ctx.session_id) or {}
    results: list[dict[str, Any]] = []
    shots: list[str] = []
    n = len(prepared)
    for i, (acc, content, warnings) in enumerate(prepared):
        label = f"{social.PLATFORMS[acc['platform']]['label']}「{acc['name']}」"
        post = state.db.create_post(acc["id"], acc["platform"], content, status="pending",
                                    session_id=ctx.session_id, task_id=session.get("task_id"))
        entry: dict[str, Any] = {"account": label, "post_id": post["id"]}
        if mode == "queue":
            entry["status"] = STATUS_LABEL["pending"]
        else:
            async def prog(f: float, t: str, i: int = i, label: str = label) -> None:
                await ctx.progress((i + f) / n, f"{label}：{t}")

            done = await social.publish_post(post["id"], prog)
            entry["status"] = STATUS_LABEL.get(done["status"], done["status"])
            for k in ("url", "error", "note"):
                if done.get(k):
                    entry[k] = done[k]
            if done.get("note") and "截图：" in done["note"]:
                shots.append(done["note"].split("截图：", 1)[1].strip())
        if warnings:
            entry["warnings"] = warnings
        results.append(entry)

    out: dict[str, Any] = {"ok": any(r["status"] in ("已发布", "待确认") for r in results), "results": results}
    if mode == "queue":
        out["message"] = "已放进「发布 → 待确认」，用户在 AutoStudio 发布页确认后才会发出。"
        if ctx.headless:    # nobody is watching this run: ask on the phone now, not only when the run ends
            social.announce_pending(ctx.session_id)
            out["message"] += "用户的手机已经收到确认通知。"
    if shots:
        out["images"] = shots
    return out


@tool("social_posts", "查看最近的发布记录（待确认 / 已发布 / 失败），包括链接和失败原因。",
      obj(status=s("只看某种状态", enum=["pending", "publishing", "published", "failed", "cancelled"]),
          limit=integer("条数，默认 20")), category="social")
async def social_posts(ctx: ToolContext, status: str | None = None, limit: int = 20) -> dict[str, Any]:
    posts = social.list_posts(status, max(1, min(100, int(limit or 20))))
    return {"posts": [{"id": p["id"], "account": f"{p['platform_label']}「{p['account_name']}」",
                       "status": STATUS_LABEL.get(p["status"], p["status"]), "title": p["content"].get("title"),
                       "url": p.get("url"), "error": p.get("error"), "note": p.get("note")} for p in posts]}
