"""Social publishing: multiple accounts per platform, a review queue, and the publishers.

Platforms: YouTube (official Data API) and 小红书 (browser automation, see xiaohongshu.py).
A publish request becomes one post per target account. Posts are either published right away or parked
as "pending" until the user approves them on the 发布 page (scheduled tasks do this unless they were
created with auto-publish on).
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import state
from ..config import SOCIAL_DIR
from ..media.ffmpeg import IMAGE_EXTS, VIDEO_EXTS, ffmpeg_bin
from . import xiaohongshu as xhs
from . import youtube as yt
from .browser import playwright_available

log = logging.getLogger("autostudio.social")

PLATFORMS = {
    "youtube": {"label": "YouTube", "video": True, "images": False},
    "xiaohongshu": {"label": "小红书", "video": True, "images": True},
}
ALIASES = {"youtube": "youtube", "yt": "youtube", "油管": "youtube", "youtube shorts": "youtube",
           "xiaohongshu": "xiaohongshu", "xhs": "xiaohongshu", "小红书": "xiaohongshu", "red": "xiaohongshu", "rednote": "xiaohongshu"}
DEBUG_DIR = SOCIAL_DIR / "debug"

Progress = Callable[[float, str], Awaitable[None]]

_locks: dict[str, asyncio.Lock] = {}
PROGRESS: dict[str, dict[str, Any]] = {}     # post id -> {"progress", "text"} while publishing
JOBS: dict[str, dict[str, Any]] = {}         # login / test jobs polled by the UI


async def _noop(_f: float, _t: str) -> None:
    return None


def _lock(aid: str) -> asyncio.Lock:
    return _locks.setdefault(aid, asyncio.Lock())


def profile_dir(aid: str) -> Path:
    return SOCIAL_DIR / "xiaohongshu" / aid


# ---------------------------------------------------------------------------
# views

def account_view(a: dict[str, Any]) -> dict[str, Any]:
    p = a.get("profile") or {}
    return {"id": a["id"], "platform": a["platform"], "platform_label": PLATFORMS.get(a["platform"], {}).get("label", a["platform"]),
            "name": a["name"], "status": a["status"], "handle": p.get("handle") or p.get("red_id") or "",
            "overseas": "rednote.com" in (p.get("site") or ""),
            "avatar": p.get("avatar") or "", "created_at": a["created_at"], "updated_at": a["updated_at"]}


def post_view(p: dict[str, Any], accounts: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    acc = (accounts or {}).get(p["account_id"]) or state.db.get_account(p["account_id"])
    return {**p, "account_name": acc["name"] if acc else "（已删除的账号）",
            "platform_label": PLATFORMS.get(p["platform"], {}).get("label", p["platform"]),
            "live": PROGRESS.get(p["id"])}


def list_posts(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    accounts = {a["id"]: a for a in state.db.list_accounts()}
    return [post_view(p, accounts) for p in state.db.list_posts(status, limit)]


# ---------------------------------------------------------------------------
# phone notifications (remote.Notices): "approve these" and how a publish went

def _phone(ev: dict[str, Any]) -> None:
    from .. import remote
    try:
        remote.remote.notices.publish(ev)
    except Exception:  # noqa: BLE001 - a notification must never break publishing
        log.warning("phone notice failed", exc_info=True)


def _label(p: dict[str, Any]) -> str:
    acc = state.db.get_account(p["account_id"])
    return f"{PLATFORMS.get(p['platform'], {}).get('label', p['platform'])}「{acc['name'] if acc else '?'}」"


def _review_route(ids: list[str]) -> str:
    return "#/review/" + ",".join(ids)


def pending_notice(session_id: str, pending: list[dict[str, Any]]) -> dict[str, Any]:
    """What a notification about posts waiting for approval says, and where tapping it goes."""
    text = "；".join(f"「{p['content'].get('title') or '无标题'}」→ {_label(p)}" for p in pending[:3])
    if len(pending) > 3:
        text += f" 等 {len(pending)} 条"
    return {"route": _review_route([p["id"] for p in pending]), "pending": len(pending),
            "posts": [{"id": p["id"], "title": p["content"].get("title") or ""} for p in pending],
            "text": text + "。点开预览，一键批准发布"}


def announce_pending(session_id: str) -> None:
    """An unattended run just queued posts: ask the user to approve them."""
    pending = state.db.list_posts("pending", 20, session_id=session_id)
    if not pending:
        return
    sess = state.db.get_session(session_id) or {}
    _phone({"type": "posts_pending", "session_id": session_id, "title": sess.get("title") or "定时任务",
            "task": sess.get("kind") == "task", **pending_notice(session_id, pending)})


def _announce_result(post: dict[str, Any], kind: str, text: str) -> None:
    sid = post.get("session_id") or ""
    waiting = [p for p in state.db.list_posts("pending", 20, session_id=sid) if p["id"] != post["id"]] if sid else []
    if waiting:
        text += f"。还有 {len(waiting)} 条等你确认"
    _phone({"type": kind, "session_id": sid, "post_id": post["id"], "title": post["content"].get("title") or "无标题",
            "text": text, "url": post.get("url") or "", "pending": len(waiting),
            "route": _review_route([post["id"], *[p["id"] for p in waiting]])})


# ---------------------------------------------------------------------------
# resolving targets & validating content

def resolve_accounts(specs: list[str]) -> list[dict[str, Any]]:
    accounts = state.db.list_accounts()
    if not accounts:
        raise ValueError("还没有添加任何社交账号。请先在 AutoStudio 左侧「发布」页面添加小红书 / YouTube 账号。")
    out: list[dict[str, Any]] = []

    def add(a: dict[str, Any]) -> None:
        if a["id"] not in {x["id"] for x in out}:
            out.append(a)

    names = "、".join(f"{a['name']}（{PLATFORMS[a['platform']]['label']}，id={a['id']}）" for a in accounts)
    for spec in specs or []:
        s = str(spec).strip()
        low = s.lower()
        if not s:
            continue
        if low in ("all", "全部", "所有", "全部账号"):
            for a in accounts:
                add(a)
        elif low in ALIASES:
            hits = [a for a in accounts if a["platform"] == ALIASES[low]]
            if not hits:
                raise ValueError(f"还没有添加{PLATFORMS[ALIASES[low]]['label']}账号。现有账号：{names}")
            for a in hits:
                add(a)
        else:
            hits = ([a for a in accounts if a["id"] == s] or [a for a in accounts if a["name"].lower() == low]
                    or [a for a in accounts if low in a["name"].lower()])
            if len(hits) != 1:
                raise ValueError(f"{'找不到' if not hits else '有多个账号匹配'}「{s}」。现有账号：{names}")
            add(hits[0])
    if not out:
        raise ValueError(f"请指定要发布到哪些账号。现有账号：{names}")
    return out


def _file(p: str | None, exts: set[str], kind: str) -> str | None:
    if not p:
        return None
    path = Path(p)
    if not path.is_file():
        raise ValueError(f"{kind}文件不存在：{p}")
    if path.suffix.lower() not in exts:
        raise ValueError(f"不支持的{kind}格式：{path.name}")
    return str(path.resolve())


def prepare(platform: str, raw: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """Normalize one platform's content; truncation is reported through `warnings`."""
    label = PLATFORMS[platform]["label"]
    video = _file(raw.get("video"), VIDEO_EXTS, "视频")
    images = [_file(i, IMAGE_EXTS, "图片") for i in raw.get("images") or []]
    cover = _file(raw.get("cover"), IMAGE_EXTS, "封面")
    title = str(raw.get("title") or "").strip()
    desc = str(raw.get("description") or "").strip()
    tags = [str(t).strip().lstrip("#").strip() for t in raw.get("tags") or [] if str(t).strip().lstrip("#").strip()]

    def cut(text: str, n: int, what: str) -> str:
        if len(text) > n:
            warnings.append(f"{label}{what}超过 {n} 字，已截断")
            return text[:n]
        return text

    if platform == "youtube":
        if not video:
            raise ValueError("YouTube 只能发布视频（需要 video 参数）")
        privacy = raw.get("privacy") or "public"
        if privacy not in yt.PRIVACY:
            raise ValueError(f"YouTube 可见性只能是 {'/'.join(yt.PRIVACY)}")
        return {"video": video, "title": cut(title or Path(video).stem, yt.TITLE_MAX, "标题"),
                "description": cut(desc, yt.DESC_MAX, "简介"), "tags": tags, "cover": cover, "privacy": privacy,
                "made_for_kids": bool(raw.get("made_for_kids")), "category_id": str(raw.get("category_id") or "22")}
    # 小红书
    if video and images:
        raise ValueError("小红书一篇笔记只能是视频或图文，二选一")
    if not video and not images:
        raise ValueError("小红书需要一个视频（video）或 1~18 张图片（images）")
    if len(images) > xhs.IMAGES_MAX:
        raise ValueError(f"小红书图文最多 {xhs.IMAGES_MAX} 张图片")
    if not title:
        raise ValueError("小红书笔记需要标题（不超过 20 字）")
    return {"video": video, "images": images, "cover": cover, "title": cut(title, xhs.TITLE_MAX, "标题"),
            "description": cut(desc, xhs.DESC_MAX, "正文"), "tags": tags[:10]}


# ---------------------------------------------------------------------------
# publishing

def youtube_client() -> tuple[str, str]:
    cfg = ((state.settings.get().get("social") or {}).get("youtube") or {})
    cid, sec = (cfg.get("client_id") or "").strip(), (cfg.get("client_secret") or "").strip()
    if not cid or not sec:
        raise RuntimeError("还没有配置 YouTube OAuth 客户端：请在「发布」页点击「添加 YouTube 账号」按引导设置。")
    return cid, sec


async def _youtube_token(acc: dict[str, Any]) -> str:
    cid, sec = youtube_client()
    return await yt.access_token(acc["secret"], cid, sec, lambda s: state.db.update_account(acc["id"], secret=s))


async def _run_xhs(acc: dict[str, Any], content: dict[str, Any], progress: Progress, *, dry_run: bool = False,
                   headless: bool | None = None) -> dict[str, Any]:
    loop = asyncio.get_running_loop()

    def cb(f: float, t: str) -> None:
        loop.call_soon_threadsafe(lambda: loop.create_task(progress(f, t)))

    if headless is None:
        headless = not (state.settings.get().get("social") or {}).get("xhs_show_browser", True)
    profile = acc.get("profile") or {}
    res = await asyncio.to_thread(xhs.publish, profile_dir(acc["id"]), content, headless=headless, progress=cb,
                                  debug_dir=DEBUG_DIR, dry_run=dry_run, site=profile.get("site"))
    if res.get("site") and res["site"] != profile.get("site"):     # the platform moved the account: remember
        state.db.update_account(acc["id"], profile={**profile, "site": res["site"]})
    return res


async def publish_post(post_id: str, progress: Progress = _noop, announce: bool = False) -> dict[str, Any]:
    """Publish one stored post now; never raises — the outcome is written to the post.

    `announce`: tell paired phones how it went (for publishes the user started and walked away from).
    """
    post = state.db.get_post(post_id)
    if not post:
        raise ValueError("发布记录不存在")
    try:
        done = await _publish(post, progress, announce)
    finally:
        PROGRESS.pop(post_id, None)     # never leave a phone waiting on a publish that is over
    if announce:
        label = _label(done)
        if done["status"] == "published":
            _announce_result(done, "post_published", f"已发到{label}")
        else:
            _announce_result(done, "post_failed", f"{label}：{done.get('error') or '发布失败'}")
    return done


async def _publish(post: dict[str, Any], progress: Progress, announce: bool) -> dict[str, Any]:
    post_id = post["id"]
    acc = state.db.get_account(post["account_id"])
    if not acc:
        return state.db.update_post(post_id, status="failed", error="账号已被删除") or post
    asked = False

    async def prog(f: float, t: str) -> None:
        nonlocal asked
        PROGRESS[post_id] = {"progress": round(f, 3), "text": t}
        if announce and not asked and "安全验证" in t:     # only someone at the PC can do this
            asked = True
            _announce_result(post, "post_attention", t)
        await progress(f, t)

    async with _lock(acc["id"]):
        state.db.update_post(post_id, status="publishing", error=None)
        PROGRESS[post_id] = {"progress": 0, "text": "准备中…"}
        try:
            if acc["platform"] == "youtube":
                res = await yt.upload(lambda: _youtube_token(acc), post["content"], prog)
            else:
                res = await _run_xhs(acc, post["content"], prog)
        except (yt.NeedLogin, xhs.NeedLogin) as e:
            state.db.update_account(acc["id"], status="expired")
            return state.db.update_post(post_id, status="failed", error=str(e)) or post
        except xhs.PublishFailed as e:
            return state.db.update_post(post_id, status="failed", error=str(e),
                                        note=f"出错时的页面截图：{e.screenshot}" if e.screenshot else None) or post
        except Exception as e:  # noqa: BLE001
            log.warning("publish %s failed: %s", post_id, e)
            return state.db.update_post(post_id, status="failed", error=str(e) or type(e).__name__) or post
        finally:
            PROGRESS.pop(post_id, None)
        if acc["status"] != "ok":
            state.db.update_account(acc["id"], status="ok")
        return state.db.update_post(post_id, status="published", url=res.get("url"),
                                    note="；".join(res.get("warnings") or []) or None) or post


def publish_in_background(post_id: str) -> None:
    state.db.update_post(post_id, status="publishing", error=None)
    PROGRESS[post_id] = {"progress": 0, "text": "排队中…"}      # counts as work in progress for phones right away
    asyncio.get_running_loop().create_task(publish_post(post_id, announce=True))


def cancel_account_posts(aid: str) -> None:
    for p in state.db.list_posts("pending", 1000):
        if p["account_id"] == aid:
            state.db.update_post(p["id"], status="cancelled")


# ---------------------------------------------------------------------------
# accounts: YouTube

def youtube_login_url(base_url: str, relogin: str | None = None) -> str:
    cid, _ = youtube_client()
    return yt.youtube_auth.start(cid, base_url.rstrip("/") + yt.CALLBACK_PATH, relogin)


async def youtube_finish_login(state_param: str, code: str) -> dict[str, Any]:
    cid, sec = youtube_client()
    record, relogin = await yt.youtube_auth.finish(state_param, code, cid, sec)
    info = await yt.channel_info(record["access_token"])
    record["channel_id"] = info["channel_id"]
    profile = {"handle": info["handle"], "avatar": info["avatar"], "channel_id": info["channel_id"], "title": info["title"]}
    same = [a for a in state.db.list_accounts("youtube")
            if (a["profile"] or {}).get("channel_id") == info["channel_id"] or (a["id"] == relogin and not (a["profile"] or {}).get("channel_id"))]
    if same:
        return state.db.update_account(same[0]["id"], secret=record, profile=profile, status="ok") or same[0]
    return state.db.create_account("youtube", info["title"] or "YouTube 频道", profile, record)


# ---------------------------------------------------------------------------
# accounts: 小红书 (login happens in a browser window the user interacts with)

def _job(kind: str, **extra: Any) -> dict[str, Any]:
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "status": "running", "error": "", "started": time.time(), **extra}
    JOBS[job["id"]] = job
    for k in [k for k, j in JOBS.items() if time.time() - j["started"] > 3600]:
        JOBS.pop(k, None)
    return job


def start_xhs_login(account_id: str | None = None) -> dict[str, Any]:
    if not playwright_available():
        raise RuntimeError("缺少浏览器自动化组件 playwright：重新双击 AutoStudio.bat 会自动安装，或运行 pip install playwright")
    running = [j for j in JOBS.values() if j["kind"] == "xhs_login" and j["status"] == "running"]
    if running:
        return running[0]
    new = account_id is None
    aid = account_id or uuid.uuid4().hex[:10]
    job = _job("xhs_login", account_id=aid)

    async def run() -> None:
        try:
            async with _lock(aid):
                prof = await asyncio.to_thread(xhs.login, profile_dir(aid), progress=lambda f, t: job.update(progress=f, text=t))
            profile = {k: v for k, v in prof.items() if v}
            twin = next((a for a in state.db.list_accounts("xiaohongshu") if a["id"] != aid and profile.get("user_id")
                         and (a["profile"] or {}).get("user_id") == profile["user_id"]), None)
            if new and twin:
                # The same 小红书 account added again: treat it as a re-login of the existing entry.
                async with _lock(twin["id"]):
                    shutil.rmtree(profile_dir(twin["id"]), ignore_errors=True)
                    profile_dir(aid).rename(profile_dir(twin["id"]))
                state.db.update_account(twin["id"], status="ok", profile={**(twin["profile"] or {}), **profile})
                job.update(account_id=twin["id"], note=f"「{twin['name']}」已经添加过了，已更新它的登录")
            elif new:
                n = len(state.db.list_accounts("xiaohongshu")) + 1
                state.db.create_account("xiaohongshu", prof.get("nickname") or f"小红书账号 {n}", profile, account_id=aid)
            else:
                old = state.db.get_account(aid)
                state.db.update_account(aid, status="ok", profile={**((old or {}).get("profile") or {}), **profile})
            job["status"] = "done"
        except xhs.LoginCancelled as e:
            job.update(status="cancelled", error=str(e))
        except Exception as e:  # noqa: BLE001
            log.warning("xhs login failed: %s", e)
            job.update(status="failed", error=str(e) or type(e).__name__)
        if new and job["status"] != "done":
            shutil.rmtree(profile_dir(aid), ignore_errors=True)

    asyncio.get_running_loop().create_task(run())
    return job


def _test_video() -> Path:
    out = DEBUG_DIR / "autostudio-test.mp4"
    if not out.exists():
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i", "color=c=0xff2442:s=720x1280:d=5:r=30",
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", str(out)], check=True, capture_output=True, timeout=120)
    return out


def start_xhs_test(aid: str) -> dict[str, Any]:
    """Dry run: upload a 5-second test clip and fill in the form, but stop before clicking 发布."""
    acc = state.db.get_account(aid)
    if not acc or acc["platform"] != "xiaohongshu":
        raise ValueError("账号不存在")
    job = _job("xhs_test", account_id=aid)

    async def run() -> None:
        try:
            video = await asyncio.to_thread(_test_video)
            content = {"video": str(video), "images": [], "cover": None, "title": "AutoStudio 测试（不会发布）",
                       "description": "这是 AutoStudio 的试运行，只填写不发布。", "tags": ["AutoStudio"]}

            async def prog(f: float, t: str) -> None:
                job.update(progress=f, text=t)

            async with _lock(aid):
                res = await _run_xhs(acc, content, prog, dry_run=True, headless=False)
            job.update(status="done", screenshot=res.get("screenshot", ""), warnings=res.get("warnings") or [])
            state.db.update_account(aid, status="ok")
        except xhs.NeedLogin as e:
            state.db.update_account(aid, status="expired")
            job.update(status="failed", error=str(e))
        except xhs.PublishFailed as e:
            job.update(status="failed", error=str(e), screenshot=e.screenshot)
        except Exception as e:  # noqa: BLE001
            job.update(status="failed", error=str(e) or type(e).__name__)

    asyncio.get_running_loop().create_task(run())
    return job


async def check_account(aid: str) -> dict[str, Any]:
    acc = state.db.get_account(aid)
    if not acc:
        raise ValueError("账号不存在")
    async with _lock(aid):
        try:
            if acc["platform"] == "youtube":
                info = await yt.channel_info(await _youtube_token(acc))
                profile = {**acc["profile"], "handle": info["handle"], "avatar": info["avatar"], "title": info["title"]}
                acc = state.db.update_account(aid, status="ok", profile=profile) or acc
            else:
                prof = await asyncio.to_thread(xhs.check, profile_dir(aid), (acc["profile"] or {}).get("site"))
                if prof is None:
                    acc = state.db.update_account(aid, status="expired") or acc
                else:
                    profile = {**acc["profile"], **{k: v for k, v in prof.items() if v}}
                    acc = state.db.update_account(aid, status="ok", profile=profile) or acc
        except (yt.NeedLogin, xhs.NeedLogin):
            acc = state.db.update_account(aid, status="expired") or acc
    return account_view(acc)


def delete_account(aid: str) -> None:
    cancel_account_posts(aid)
    state.db.delete_account(aid)
    shutil.rmtree(profile_dir(aid), ignore_errors=True)
