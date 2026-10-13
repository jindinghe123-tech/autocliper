"""Playwright helpers for platforms without an upload API (小红书).

Each account gets its own persistent browser profile, so logins survive restarts and accounts never mix.
We drive the browser the user already has (Edge on Windows, Chrome on macOS) so nothing extra has to be
downloaded; only when neither exists is Playwright's bundled Chromium installed on demand.
All functions here are synchronous and must run in a worker thread (asyncio.to_thread).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ARGS = ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate"]
CHANNELS = {"win32": ["msedge", "chrome", None], "darwin": ["chrome", "msedge", None]}.get(sys.platform, [None, "chrome", "msedge"])
DOWNLOAD_MIRRORS = [None, "https://npmmirror.com/mirrors/playwright"]


class BrowserMissing(RuntimeError):
    pass


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _require() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        raise BrowserMissing("发布到小红书需要浏览器自动化组件 playwright：重新双击 AutoStudio.bat 会自动安装，"
                             "或手动运行 pip install playwright") from e
    return sync_playwright


def _install_chromium() -> None:
    last = ""
    for host in DOWNLOAD_MIRRORS:
        env = {**os.environ, **({"PLAYWRIGHT_DOWNLOAD_HOST": host} if host else {})}
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], env=env,
                           capture_output=True, text=True, timeout=900)
        if r.returncode == 0:
            return
        last = (r.stderr or r.stdout)[-300:]
    raise BrowserMissing(f"没有找到 Edge / Chrome，自动下载 Chromium 也失败了：{last}")


def launch(p: Any, profile_dir: Path, headless: bool) -> Any:
    """Open a persistent context for one account's profile, preferring the system browser."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    opts: dict[str, Any] = dict(headless=headless, args=ARGS, ignore_default_args=["--enable-automation"],
                                viewport={"width": 1400, "height": 900}, locale="zh-CN", accept_downloads=False)
    errors: list[str] = []
    for attempt in range(2):
        for ch in CHANNELS:
            try:
                return p.chromium.launch_persistent_context(str(profile_dir), channel=ch, **opts)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "user data directory is already in use" in msg.lower() or "ProcessSingleton" in msg:
                    raise RuntimeError("这个账号的浏览器窗口已经打开着（可能正在登录或发布），请先关闭它再试。") from e
                errors.append(f"{ch or 'chromium'}: {msg.splitlines()[0][:160]}")
        if attempt == 0:
            _install_chromium()
    raise BrowserMissing("无法启动浏览器：" + "；".join(errors))


def run(fn: Any, profile_dir: Path, headless: bool) -> Any:
    """Launch → fn(context, page) → always close."""
    sync_playwright = _require()
    with sync_playwright() as p:
        ctx = launch(p, profile_dir, headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(20000)
            return fn(ctx, page)
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001 - the user may have closed the window already
                pass
