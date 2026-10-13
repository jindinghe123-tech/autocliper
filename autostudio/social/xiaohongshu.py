"""小红书 publishing through the creator platform (creator.xiaohongshu.com).

Personal accounts have no upload API, so we drive a real browser with the account's own persistent profile:
the user signs in once (QR code / SMS) in a window we open, and later posts reuse that login.
The creator site changes from time to time, so every step has fallbacks and failures save a screenshot.
Everything here is synchronous; call it from a worker thread.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import browser

CREATOR = "https://creator.xiaohongshu.com"
LOGIN_URL = f"{CREATOR}/login"
HOME_URL = f"{CREATOR}/new/home"
PUBLISH_URL = f"{CREATOR}/publish/publish?from=menu&target={{target}}"
NOTES_URL = f"{CREATOR}/new/note-manager"
TITLE_MAX = 20
DESC_MAX = 1000
IMAGES_MAX = 18

Progress = Callable[[float, str], None]


class NeedLogin(RuntimeError):
    pass


class LoginCancelled(RuntimeError):
    pass


class PublishFailed(RuntimeError):
    def __init__(self, msg: str, screenshot: str = ""):
        super().__init__(msg)
        self.screenshot = screenshot


def _logged_in(url: str) -> bool:
    u = urlparse(url)
    return u.netloc.endswith("xiaohongshu.com") and not u.path.startswith("/login")


def _settle(page: Any, idle_ms: int = 6000) -> None:
    try:
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=idle_ms)
    except Exception:  # noqa: BLE001 - long-polling pages never go idle; that's fine
        pass


def _text(page: Any) -> str:
    try:
        return page.inner_text("body", timeout=5000)
    except Exception:  # noqa: BLE001
        return ""


def _visible(page: Any, selector: str) -> Any | None:
    loc = page.locator(selector).filter(visible=True)
    return loc.first if loc.count() else None


# The creator site's own profile endpoint first; the page's top bar as a fallback.
PROFILE_JS = r"""async () => {
  try {
    const r = await fetch('/api/galaxy/user/info', { credentials: 'include' });
    const d = (await r.json()).data || {};
    if (d.userName) return { nickname: d.userName, avatar: d.userAvatar || '', red_id: d.redId || '', user_id: d.userId || '' };
  } catch (e) {}
  const pick = (s) => (document.querySelector(s)?.innerText || '').trim().split('\n')[0];
  const img = [...document.querySelectorAll('img')].find(i => /avatar/i.test(i.src));
  const red = (document.body.innerText.match(/小红书(?:账)?号[:：]\s*([\w.-]+)/) || [])[1] || '';
  return { nickname: pick('.account-name') || pick('.user-info') || pick('.name-box'), avatar: img ? img.src : '',
           red_id: red, user_id: '' };
}"""


def _profile(page: Any) -> dict[str, Any]:
    try:
        page.wait_for_timeout(1500)
        return page.evaluate(PROFILE_JS) or {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# login

def login(profile_dir: Path, timeout: float = 600) -> dict[str, Any]:
    """Open a visible window on the login page and wait until the user has signed in (or closes it)."""

    def go(ctx: Any, page: Any) -> dict[str, Any]:
        page.goto(LOGIN_URL)
        deadline = time.time() + timeout
        gone_since = 0.0
        while time.time() < deadline:
            # Edge may keep extra hidden pages (and keep running after the window is closed), so look at
            # every page and treat "no 小红书 page left" as the user closing the window.
            pages = [p for p in ctx.pages if not p.is_closed() and "xiaohongshu.com" in p.url]
            if not pages:
                gone_since = gone_since or time.time()
                if time.time() - gone_since > 4:
                    raise LoginCancelled("登录窗口已关闭")
                time.sleep(1)
                continue
            gone_since = 0.0
            for page in pages:
                try:
                    if _logged_in(page.url) and "creator.xiaohongshu.com" in page.url:
                        page.goto(HOME_URL)
                        _settle(page)
                        if _logged_in(page.url):
                            return _profile(page)
                except Exception as e:  # noqa: BLE001 - window closed mid-navigation
                    if "closed" in str(e).lower():
                        raise LoginCancelled("登录窗口已关闭") from e
            time.sleep(1)
        raise LoginCancelled("登录超时（10 分钟）")

    try:
        return browser.run(go, profile_dir, headless=False)
    except LoginCancelled:
        raise
    except Exception as e:  # noqa: BLE001
        if "closed" in str(e).lower():
            raise LoginCancelled("登录窗口已关闭") from e
        raise


def check(profile_dir: Path) -> dict[str, Any] | None:
    """Profile info if this browser profile is still signed in, else None."""

    def go(_ctx: Any, page: Any) -> dict[str, Any] | None:
        page.goto(HOME_URL)
        _settle(page)
        return _profile(page) if _logged_in(page.url) else None

    return browser.run(go, profile_dir, headless=True)


# ---------------------------------------------------------------------------
# publish steps

def _security_check(page: Any) -> bool:
    t = _text(page)
    return any(k in t for k in ("安全验证", "滑块验证", "请完成验证", "拖动滑块"))


def _wait_human(page: Any, headless: bool, progress: Progress) -> None:
    if not _security_check(page):
        return
    if headless:
        raise RuntimeError("小红书要求安全验证。请在 发布 页打开「发布时显示浏览器窗口」，下次发布时在弹出的窗口里手动完成验证。")
    progress(0.05, "小红书要求安全验证，请在弹出的浏览器窗口里完成…")
    deadline = time.time() + 300
    while time.time() < deadline and _security_check(page):
        time.sleep(2)
    if _security_check(page):
        raise RuntimeError("安全验证超时未完成")


def _select_tab(page: Any, label: str) -> None:
    try:
        tab = page.get_by_text(label, exact=True).filter(visible=True)
        if tab.count():
            tab.first.click()
            page.wait_for_timeout(800)
    except Exception:  # noqa: BLE001 - the URL already selects the right tab on most versions
        pass


def _file_input(page: Any, video: bool) -> Any:
    page.wait_for_selector("input[type=file]", state="attached", timeout=30000)
    inputs = page.locator("input[type=file]")
    want = ("video", ".mp4", ".mov") if video else ("image", ".jpg", ".png", ".webp")
    for i in range(inputs.count()):
        accept = (inputs.nth(i).get_attribute("accept") or "").lower()
        if any(w in accept for w in want):
            return inputs.nth(i)
    return inputs.first


def _title_box(page: Any) -> Any | None:
    return (_visible(page, "input[placeholder*='标题']") or _visible(page, ".title-container input")
            or _visible(page, "input.d-text"))


def _wait_upload(page: Any, video: bool, size_mb: float, headless: bool, progress: Progress) -> None:
    deadline = time.time() + max(240, size_mb * 8)
    started = time.time()
    quiet = 0
    while time.time() < deadline:
        _wait_human(page, headless, progress)
        t = _text(page)
        if "上传失败" in t:
            raise RuntimeError("小红书提示上传失败（文件格式、时长或大小可能不符合要求）")
        uploading = re.search(r"上传中|正在上传|处理中|转码中", t)
        pct = re.search(r"(\d{1,3})(?:\.\d+)?\s*%", t) if uploading else None
        if pct:
            progress(0.1 + 0.6 * min(100, int(pct.group(1))) / 100, f"上传中 {pct.group(1)}%")
        has_form = _title_box(page) is not None
        if not uploading and has_form:
            if not video or "上传成功" in t:
                page.wait_for_timeout(1500)
                return
            quiet += 1
            if quiet >= 4 and time.time() - started > 8:   # no progress text for a while: assume done
                return
        else:
            quiet = 0
        time.sleep(1.5)
    raise RuntimeError("上传超时")


def _fill_title(page: Any, title: str) -> None:
    box = _title_box(page)
    if box is None:
        raise RuntimeError("没有找到标题输入框（小红书页面可能改版了）")
    box.click()
    box.fill(title)


def _editor(page: Any) -> Any:
    for sel in ("div.ql-editor", "div.tiptap", "div.ProseMirror", "[contenteditable='true']"):
        el = _visible(page, sel)
        if el is not None:
            return el
    raise RuntimeError("没有找到正文输入框（小红书页面可能改版了）")


TOPIC_ITEMS = "#creator-editor-topic-container .item"


def _pick_topic(page: Any, tag: str) -> bool:
    """Choose the suggestion whose name is exactly `tag` (it becomes a real, linked topic)."""
    try:
        page.wait_for_selector(TOPIC_ITEMS, state="visible", timeout=6000)
        page.wait_for_timeout(500)   # the list refreshes once suggestions arrive
    except Exception:  # noqa: BLE001 - no suggestions: keep it as plain text
        return False
    items = page.locator(TOPIC_ITEMS).filter(visible=True)
    for i in range(min(items.count(), 12)):
        name = re.sub(r"\s*(新建话题|[\d.]+[万亿]?浏览)\s*$", "", items.nth(i).inner_text().replace("\n", " ")).strip()
        if name.lstrip("#").lower() == tag.lower():
            items.nth(i).click()
            page.wait_for_timeout(300)
            return True
    return False


def _fill_body(page: Any, desc: str, tags: list[str]) -> None:
    ed = _editor(page)
    ed.click()
    for i, line in enumerate(desc.split("\n")):
        if i:
            page.keyboard.press("Enter")
        if line:
            page.keyboard.insert_text(line)
    if tags:
        if desc.strip():
            page.keyboard.press("Enter")
        for tag in tags:
            page.keyboard.type("#" + tag, delay=60)
            if not _pick_topic(page, tag):
                page.keyboard.type(" ")
                if page.locator(TOPIC_ITEMS).filter(visible=True).count():
                    page.keyboard.press("Escape")


DISMISS_JS = r"""() => {
  // one-off feature tips ("视频可以PK封面啦…") float above the form; close them, never touch the form itself
  let n = 0;
  for (const el of document.querySelectorAll('[class*="close" i]')) {
    const box = el.closest('.tippy-box, [class*="popover" i], [class*="guide" i], [class*="tip" i]');
    if (box && el.offsetParent !== null && !box.closest('#publish-container') && !box.querySelector('#creator-editor-topic-container')) {
      el.click(); n++;
    }
  }
  return n;
}"""


def _dismiss_tips(page: Any) -> None:
    try:
        for label in ("我知道了", "知道了", "下次再说"):
            loc = page.get_by_text(label, exact=True).filter(visible=True)
            for i in range(min(loc.count(), 3)):
                loc.nth(i).click()
        page.evaluate(DISMISS_JS)
    except Exception:  # noqa: BLE001 - purely cosmetic
        pass


def _set_cover(page: Any, cover: str) -> str | None:
    """Best effort; returns a warning on failure (the default first-frame cover stays)."""
    try:
        btn = None
        for label in ("设置封面", "修改封面", "编辑封面", "选择封面"):
            loc = page.get_by_text(label, exact=True).filter(visible=True)
            if loc.count():
                btn = loc.first
                break
        if btn is None:
            return "没有找到设置封面的按钮，已使用默认封面"
        btn.click()
        page.wait_for_timeout(1200)
        up = page.get_by_text("上传封面", exact=True).filter(visible=True)
        if up.count():
            up.first.click()
            page.wait_for_timeout(800)
        inputs = page.locator("input[type=file][accept*='image']")
        if not inputs.count():
            raise RuntimeError("no image input")
        inputs.last.set_input_files(cover)
        page.wait_for_timeout(2500)
        for label in ("确定", "完成", "确认"):
            ok = page.get_by_role("button", name=label, exact=True).filter(visible=True)
            if ok.count():
                ok.last.click()
                page.wait_for_timeout(1500)
                return None
        raise RuntimeError("no confirm button")
    except Exception as e:  # noqa: BLE001
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        return f"封面设置失败（{str(e)[:60]}），已使用默认封面"


def _click_publish(page: Any) -> None:
    buttons = page.locator("button").filter(visible=True)
    for i in range(buttons.count()):
        b = buttons.nth(i)
        try:
            if (b.inner_text() or "").strip() == "发布" and b.is_enabled():
                b.scroll_into_view_if_needed()
                b.click()
                return
        except Exception:  # noqa: BLE001
            continue
    loc = page.get_by_text("发布", exact=True).filter(visible=True)
    if loc.count():
        loc.last.click()
        return
    raise RuntimeError("没有找到「发布」按钮（小红书页面可能改版了）")


def _wait_published(page: Any, headless: bool, progress: Progress) -> None:
    deadline = time.time() + 90
    while time.time() < deadline:
        _wait_human(page, headless, progress)
        if "success" in page.url or "发布成功" in _text(page):
            return
        time.sleep(1)
    toast = ""
    try:
        toast = page.locator("[class*='toast' i], [class*='message' i], [class*='error' i]").filter(visible=True).first.inner_text(timeout=1000)
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError("点击发布后没有看到「发布成功」" + (f"，页面提示：{toast.strip()[:80]}" if toast.strip() else ""))


def _shot(page: Any, debug_dir: Path, tag: str) -> str:
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"xiaohongshu-{time.strftime('%Y%m%d-%H%M%S')}-{tag}.png"
        page.screenshot(path=str(path), full_page=True, timeout=10000)
        return str(path)
    except Exception:  # noqa: BLE001
        return ""


def publish(profile_dir: Path, post: dict[str, Any], *, headless: bool, progress: Progress, debug_dir: Path,
            dry_run: bool = False) -> dict[str, Any]:
    video = post.get("video")
    images = post.get("images") or []
    files = [video] if video else images
    size_mb = sum(Path(f).stat().st_size for f in files) / 1048576
    warnings: list[str] = []

    def go(_ctx: Any, page: Any) -> dict[str, Any]:
        progress(0.02, "打开小红书创作中心…")
        page.goto(PUBLISH_URL.format(target="video" if video else "image"))
        _settle(page)
        if not _logged_in(page.url):
            raise NeedLogin("小红书登录已失效，请在 发布 页重新登录这个账号")
        try:
            _wait_human(page, headless, progress)
            _select_tab(page, "上传视频" if video else "上传图文")
            _file_input(page, bool(video)).set_input_files(files)
            progress(0.1, "上传中…")
            _wait_upload(page, bool(video), size_mb, headless, progress)
            _dismiss_tips(page)
            progress(0.72, "填写标题和正文…")
            _fill_title(page, post.get("title") or "")
            _fill_body(page, post.get("description") or "", post.get("tags") or [])
            if video and post.get("cover"):
                progress(0.8, "设置封面…")
                w = _set_cover(page, post["cover"])
                if w:
                    warnings.append(w)
            if dry_run:
                return {"dry_run": True, "screenshot": _shot(page, debug_dir, "dryrun"), "warnings": warnings}
            progress(0.88, "点击发布…")
            _dismiss_tips(page)
            _click_publish(page)
            _wait_published(page, headless, progress)
            return {"url": NOTES_URL, "warnings": warnings}
        except NeedLogin:
            raise
        except Exception as e:  # noqa: BLE001
            raise PublishFailed(str(e) or type(e).__name__, _shot(page, debug_dir, "error")) from e

    return browser.run(go, profile_dir, headless)
