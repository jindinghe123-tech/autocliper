"""Automatic updates from the project website.

Every few hours the app reads <website>/update.json (written at deploy time by scripts/build-site.mjs). When
a newer version is out it downloads the zip, checks its SHA-256, unpacks it into <data>/update/ and marks it
ready. Then it waits until nothing is going on — no agent run, no publish, no scheduled task due soon, and
nobody using the app on this PC for a while — and restarts; launcher.py copies the new files in on the way
back up. That only happens when launcher.py runs the app (the normal installed setup) and never in a git
checkout; otherwise the app just says that a new version is out.

The Android app updates from the PC: the zip carries the newest APK (autostudio/app), see remote.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from . import relay, state
from .config import VERSION
from .launcher import APPLIED_FILE, READY_FILE, RESTART, UPDATE_DIR, is_checkout

log = logging.getLogger("autostudio.updater")

DEFAULT_URL = relay.DEFAULT_URL + "/update.json"
FIRST_CHECK_S = 120
CHECK_EVERY_S = 6 * 3600
IDLE_UI_S = 10 * 60         # nobody has used the app on this PC for this long
TASK_MARGIN_S = 15 * 60     # and no scheduled task starts within this


def parse_version(v: str) -> tuple[int, ...]:
    out = []
    for part in str(v).strip().lstrip("v").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def newer(a: str, b: str) -> bool:
    """Is version a newer than version b?"""
    return parse_version(a) > parse_version(b)


def unpack(zip_path: Path, dest: Path) -> Path:
    """Unpack the release zip (everything under AutoStudio/) into dest; returns the folder holding the app."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if not name.startswith("AutoStudio/") or name.endswith("/"):
                continue
            rel = Path(name[len("AutoStudio/"):])
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"更新包里有可疑的路径：{name}")
            out = dest / "AutoStudio" / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(out, "wb") as f:
                shutil.copyfileobj(src, f)
    app = dest / "AutoStudio"
    if not (app / "autostudio" / "__main__.py").is_file():
        raise ValueError("更新包不完整")
    return app


class Updater:
    def __init__(self) -> None:
        self.state = "idle"         # idle | checking | latest | downloading | ready | error | restarting
        self.latest: dict[str, Any] | None = None
        self.error = ""
        self.checked_at = 0.0
        self.progress = 0.0
        self._task: asyncio.Task | None = None
        self._busy = asyncio.Lock()
        self.transport: Any = None      # tests plug in a fake website here

    # -- settings --------------------------------------------------------------

    @staticmethod
    def cfg() -> dict[str, Any]:
        return {"auto": True, "url": "", **(state.settings.get().get("update") or {})}

    def url(self) -> str:
        return (self.cfg().get("url") or DEFAULT_URL).strip()

    @staticmethod
    def can_install() -> bool:
        """Only an installed copy run by launcher.py replaces its own files."""
        return state.supervised and not is_checkout()

    def view(self) -> dict[str, Any]:
        applied = None
        with contextlib.suppress(OSError, ValueError):
            a = json.loads(APPLIED_FILE.read_text("utf-8"))
            if a.get("version") == VERSION and time.time() - float(a.get("at") or 0) < 3 * 86400:
                applied = a
        return {"current": VERSION, "state": self.state, "latest": (self.latest or {}).get("version"),
                "notes": (self.latest or {}).get("notes", ""), "error": self.error, "checked_at": self.checked_at or None,
                "progress": round(self.progress, 3), "auto": bool(self.cfg().get("auto")), "can_install": self.can_install(),
                "checkout": is_checkout(), "just_updated": applied}

    # -- checking & downloading -----------------------------------------------

    async def check(self, download: bool | None = None) -> dict[str, Any]:
        async with self._busy:
            self.state, self.error = "checking", ""
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True, transport=self.transport) as c:
                    r = await c.get(self.url(), params={"v": VERSION}, headers={"Cache-Control": "no-cache"})
                    r.raise_for_status()
                    info = r.json()
                self.latest, self.checked_at = info, time.time()
                if not newer(str(info.get("version") or "0"), VERSION):
                    self.state = "latest"
                    return self.view()
                if READY_FILE.exists() and self._ready_version() == info["version"]:
                    self.state = "ready"
                    return self.view()
                want = self.can_install() and (download if download is not None else bool(self.cfg().get("auto")))
                if not want:
                    self.state = "available"
                    return self.view()
                await self._download(info)
                self.state = "ready"
            except Exception as e:  # noqa: BLE001 - offline, website down: try again later
                self.state, self.error = "error", str(e)[:200] or type(e).__name__
                log.info("update check failed: %s", self.error)
            return self.view()

    @staticmethod
    def _ready_version() -> str:
        with contextlib.suppress(OSError, ValueError):
            return str(json.loads(READY_FILE.read_text("utf-8")).get("version") or "")
        return ""

    async def _download(self, info: dict[str, Any]) -> None:
        z = info.get("zip") or {}
        if not z.get("url") or not z.get("sha256"):
            raise ValueError("update.json 缺少下载地址或校验值")
        url = httpx.URL(self.url()).join(z["url"])
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        part = UPDATE_DIR / f"AutoStudio-{info['version']}.zip.part"
        self.state, self.progress = "downloading", 0.0
        h = hashlib.sha256()
        async with httpx.AsyncClient(timeout=60, follow_redirects=True, transport=self.transport) as c:
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or z.get("size") or 0)
                done = 0
                with open(part, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if total:
                            self.progress = min(1.0, done / total)
        if h.hexdigest() != str(z["sha256"]).lower():
            part.unlink(missing_ok=True)
            raise ValueError("下载的更新包校验不通过，已丢弃")
        app = await asyncio.to_thread(unpack, part, UPDATE_DIR / "staged")
        part.unlink(missing_ok=True)
        READY_FILE.write_text(json.dumps({"version": info["version"], "dir": str(app), "notes": info.get("notes", ""),
                                          "at": time.time()}, ensure_ascii=False), "utf-8")
        log.info("update %s downloaded and ready", info["version"])

    # -- installing ------------------------------------------------------------

    @staticmethod
    def idle() -> bool:
        from . import social

        if state.runs and any(not r.done for r in state.runs.runs.values()):
            return False
        if social.PROGRESS or any(j.get("status") == "running" for j in social.JOBS.values()):
            return False
        if time.time() - state.last_ui_request < IDLE_UI_S:
            return False
        nxt = state.scheduler.seconds_to_next() if state.scheduler and state.scheduler.sched.running else None
        return nxt is None or nxt > TASK_MARGIN_S

    def install_now(self) -> bool:
        """Restart into the downloaded version (launcher.py installs it on the way back up)."""
        if not READY_FILE.exists() or not self.can_install():
            return False
        self.state = "restarting"
        log.info("restarting to install the update")
        return state.request_exit(RESTART)

    async def run(self) -> None:
        await asyncio.sleep(FIRST_CHECK_S)
        last = 0.0
        while True:
            if time.time() - last > CHECK_EVERY_S:
                last = time.time()
                if self.cfg().get("auto") or not self.latest:
                    await self.check()
            if self.state == "ready" and self.cfg().get("auto") and self.idle():
                self.install_now()
                return
            await asyncio.sleep(60)

    def start(self) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self.run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task


updater = Updater()
