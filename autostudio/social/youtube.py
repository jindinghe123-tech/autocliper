"""YouTube publishing through the official YouTube Data API v3.

Each user brings their own Google Cloud OAuth client (type "Desktop app"), because an open-source app can't
ship a verified one. Sign-in is OAuth 2.0 + PKCE with a loopback redirect to AutoStudio itself; every
channel the user signs in with becomes one account (refresh token stored locally, never sent to the browser).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
CALLBACK_PATH = "/oauth/youtube/callback"
CHUNK = 8 * 1024 * 1024          # must be a multiple of 256 KiB
TITLE_MAX = 100
DESC_MAX = 5000
TAGS_MAX_CHARS = 450
PRIVACY = ("public", "unlisted", "private")

Progress = Callable[[float, str], Awaitable[None]]

REASONS = {
    "quotaExceeded": "今天的 YouTube API 配额已用完（Google 默认配额每天只够上传少量视频），明天会自动恢复，也可以在 Google Cloud 申请提高配额",
    "uploadLimitExceeded": "这个频道今天的上传次数已达 YouTube 上限，请明天再试",
    "youtubeSignupRequired": "这个 Google 账号还没有创建 YouTube 频道，请先在 youtube.com 创建频道",
    "insufficientPermissions": "授权不完整，请重新登录这个 YouTube 账号并勾选所有权限",
    "invalidTitle": "标题不合法（不能为空、不能超过 100 字、不能包含 < >）",
    "invalidDescription": "简介不合法（不能超过 5000 字节、不能包含 < >）",
    "invalidTags": "标签不合法",
}


class NeedLogin(RuntimeError):
    pass


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _error(r: httpx.Response) -> RuntimeError:
    try:
        err = r.json().get("error") or {}
    except Exception:  # noqa: BLE001
        err = {}
    if isinstance(err, str):   # OAuth endpoint errors look like {"error": "invalid_grant", ...}
        return NeedLogin("YouTube 授权已失效，请在 发布 页重新登录这个账号") if err == "invalid_grant" \
            else RuntimeError(f"Google OAuth 错误：{err}")
    reason = ((err.get("errors") or [{}])[0]).get("reason", "")
    if r.status_code == 401:
        return NeedLogin("YouTube 授权已失效，请在 发布 页重新登录这个账号")
    msg = REASONS.get(reason) or err.get("message") or r.text[:300]
    return RuntimeError(f"YouTube API 错误（HTTP {r.status_code}{', ' + reason if reason else ''}）：{msg}")


# ---------------------------------------------------------------------------
# OAuth

class YouTubeAuth:
    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}

    def start(self, client_id: str, redirect_uri: str, relogin_account: str | None = None) -> str:
        verifier = _b64url(secrets.token_bytes(48))
        state = _b64url(secrets.token_bytes(24))
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v["started"] < 1800}
        self._pending[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "started": now,
                                "account": relogin_account}
        params = {
            "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPES,
            "access_type": "offline", "prompt": "select_account consent", "include_granted_scopes": "true",
            "state": state, "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def finish(self, state: str, code: str, client_id: str, client_secret: str) -> tuple[dict[str, Any], str | None]:
        """Exchange the code → (token record, account id being re-logged-in or None)."""
        pending = self._pending.pop(state, None)
        if not pending:
            raise RuntimeError("登录请求已过期，请回到 AutoStudio 重新点击添加账号")
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(TOKEN_URL, data={
                "code": code, "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": pending["redirect_uri"], "grant_type": "authorization_code",
                "code_verifier": pending["verifier"]})
        if r.status_code >= 400:
            raise RuntimeError(f"换取令牌失败：{r.text[:300]}")
        tok = r.json()
        if not tok.get("refresh_token"):
            raise RuntimeError("Google 没有返回 refresh_token，请到 Google 账号 → 第三方应用 中移除授权后重试")
        record = {"client_id": client_id, "refresh_token": tok["refresh_token"], "access_token": tok.get("access_token", ""),
                  "expires_at": time.time() + float(tok.get("expires_in") or 3600)}
        return record, pending.get("account")


youtube_auth = YouTubeAuth()


async def access_token(secret: dict[str, Any], client_id: str, client_secret: str,
                       save: Callable[[dict[str, Any]], None]) -> str:
    """A valid access token, refreshing (and persisting via `save`) when it is about to expire."""
    if secret.get("access_token") and float(secret.get("expires_at") or 0) - time.time() > 120:
        return secret["access_token"]
    if not secret.get("refresh_token"):
        raise NeedLogin("YouTube 账号未登录，请在 发布 页重新登录")
    if secret.get("client_id") and secret["client_id"] != client_id:
        raise NeedLogin("YouTube OAuth 客户端已更换，请重新登录这个账号")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data={"client_id": client_id, "client_secret": client_secret,
                                          "refresh_token": secret["refresh_token"], "grant_type": "refresh_token"})
    if r.status_code >= 400:
        raise _error(r)
    tok = r.json()
    secret.update(access_token=tok["access_token"], expires_at=time.time() + float(tok.get("expires_in") or 3600))
    save(secret)
    return secret["access_token"]


async def channel_info(token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{API}/channels", params={"part": "snippet", "mine": "true"},
                        headers={"Authorization": f"Bearer {token}"})
    if r.status_code >= 400:
        raise _error(r)
    items = r.json().get("items") or []
    if not items:
        raise RuntimeError(REASONS["youtubeSignupRequired"])
    sn = items[0]["snippet"]
    thumbs = sn.get("thumbnails") or {}
    return {"channel_id": items[0]["id"], "title": sn.get("title", ""), "handle": sn.get("customUrl", ""),
            "avatar": (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")}


# ---------------------------------------------------------------------------
# upload

def clean_text(s: str) -> str:
    return (s or "").replace("<", "＜").replace(">", "＞")


def fit_tags(tags: list[str]) -> list[str]:
    out, total = [], 0
    for t in tags:
        t = clean_text(t).strip().lstrip("#").replace(",", " ")
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if not t or total + cost > TAGS_MAX_CHARS:
            continue
        out.append(t)
        total += cost
    return out


async def upload(token_fn: Callable[[], Awaitable[str]], post: dict[str, Any], progress: Progress) -> dict[str, Any]:
    path = Path(post["video"])
    size = path.stat().st_size
    privacy = post.get("privacy") or "public"
    body: dict[str, Any] = {
        "snippet": {"title": clean_text(post.get("title") or path.stem)[:TITLE_MAX],
                    "description": clean_text(post.get("description") or ""),
                    "tags": fit_tags(post.get("tags") or []),
                    "categoryId": str(post.get("category_id") or "22")},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(post.get("made_for_kids")),
                   "embeddable": True},
    }
    warnings: list[str] = []
    timeout = httpx.Timeout(60, read=300, write=300)
    async with httpx.AsyncClient(timeout=timeout) as c:
        async def headers(extra: dict[str, str]) -> dict[str, str]:
            return {"Authorization": f"Bearer {await token_fn()}", **extra}

        await progress(0.02, "创建上传任务…")
        r = await c.post(f"{UPLOAD_API}/videos", params={"uploadType": "resumable", "part": "snippet,status"},
                         json=body, headers=await headers({
                             "X-Upload-Content-Length": str(size),
                             "X-Upload-Content-Type": mimetypes.guess_type(path.name)[0] or "video/*"}))
        if r.status_code >= 400:
            raise _error(r)
        session = r.headers["Location"]

        offset, failures, video = 0, 0, None
        with path.open("rb") as f:
            while video is None:
                try:
                    if offset >= size:   # everything sent but no final response yet: ask for the status
                        r = await c.put(session, headers=await headers({"Content-Range": f"bytes */{size}"}))
                    else:
                        f.seek(offset)
                        chunk = f.read(CHUNK)
                        r = await c.put(session, content=chunk, headers=await headers({
                            "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}"}))
                except httpx.TransportError as e:
                    failures += 1
                    if failures > 8:
                        raise RuntimeError(f"上传中断：{e}") from e
                    await asyncio.sleep(min(30, 2 ** failures))
                    r = await c.put(session, headers=await headers({"Content-Range": f"bytes */{size}"}))
                if r.status_code in (200, 201):
                    video = r.json()
                elif r.status_code == 308:
                    rng = r.headers.get("Range")
                    offset = int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
                    failures = 0
                    await progress(0.05 + 0.9 * offset / max(size, 1), f"上传中 {offset * 100 // max(size, 1)}%")
                elif r.status_code in (500, 502, 503, 504) and failures < 8:
                    failures += 1
                    await asyncio.sleep(min(30, 2 ** failures))
                    offset = size   # next round queries the server for how much it really has
                else:
                    raise _error(r)

        vid = video["id"]
        got = (video.get("status") or {}).get("privacyStatus")
        if privacy != "private" and got == "private":
            warnings.append("视频被 YouTube 设为了「私享」：未通过 YouTube API 审核的 Google Cloud 项目上传的视频会被锁定为私享。"
                            "可以在 YouTube 工作室手动改为公开，或在 Google 提交 API 审核后自动公开。")
        if post.get("cover"):
            await progress(0.97, "上传封面…")
            cover = Path(post["cover"])
            r = await c.post(f"{UPLOAD_API}/thumbnails/set", params={"videoId": vid, "uploadType": "media"},
                             content=cover.read_bytes(),
                             headers=await headers({"Content-Type": mimetypes.guess_type(cover.name)[0] or "image/jpeg"}))
            if r.status_code >= 400:
                warnings.append("封面上传失败（自定义封面需要频道完成手机验证，且图片不超过 2MB）：" + str(_error(r))[:120])
    return {"url": f"https://youtu.be/{vid}", "video_id": vid, "privacy": got or privacy, "warnings": warnings}
