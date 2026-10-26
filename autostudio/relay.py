"""Reach paired phones that are not on the PC's network, through an end-to-end encrypted mailbox.

On the local network the phone talks to the PC directly (remote.py). Away from it, on mobile data at a job
site, "a video is waiting for your approval" and the tap on 同意 go through a small mailbox on the project's
website instead (relay/worker.js, one Cloudflare Durable Object per PC): the PC leaves messages there for
each paired phone and collects the phones' answers.

Every message is sealed with AES-256-GCM under a key that only the PC and that one phone have. The key is
handed over at pairing, on the local network, and never reaches the mailbox, which only sees who wrote to
whom and when. Each phone also gets its own mailbox token (the mailbox stores its hash), so disconnecting
a phone on the PC cuts it off from the mailbox too.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("autostudio.relay")

DEFAULT_URL = "https://autostudio.jindinghe123.workers.dev"
API = "/relay/v1"
POLL_BUSY_S = 20          # something is waiting for the user's answer: look for it often
POLL_IDLE_S = 300
BUSY_FOR_S = 15 * 60      # after sending news, answers are likely for a while
COVER_W = 540             # cover thumbnails travel inside the message (~40 KB)

try:     # optional: without it phones simply have to be on the PC's network
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # noqa: BLE001
    AESGCM = None  # type: ignore[assignment,misc]


def available() -> bool:
    return AESGCM is not None


def _aad(box: str, device_id: str, direction: str) -> bytes:
    return f"autostudio-relay/v1|{box}|{device_id}|{direction}".encode()


def seal(key: bytes, aad: bytes, data: dict[str, Any]) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, json.dumps(data, ensure_ascii=False).encode("utf-8"), aad)
    return base64.b64encode(nonce + ct).decode()


def unseal(key: bytes, aad: bytes, body: str) -> dict[str, Any]:
    raw = base64.b64decode(body)
    out = json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], aad).decode("utf-8"))
    if not isinstance(out, dict):
        raise ValueError("not an object")
    return out


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def cover_jpeg(path: str | None, width: int = COVER_W) -> str | None:
    """A small JPEG of a cover image, base64, to show in the phone's notification."""
    if not path or not Path(path).is_file():
        return None
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((width, width * 2))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=72, optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001 - a notification without a picture is fine
        log.debug("cover thumbnail failed", exc_info=True)
        return None


Command = Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]


class Relay:
    """The PC's side of the mailbox. Lives in remote.json under "relay"; devices carry their own keys."""

    def __init__(self, access: Any):
        self.access = access          # remote.RemoteAccess: its data and save()
        self.on_command: Command | None = None
        self.status: Callable[[], dict[str, Any]] = lambda: {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._outbox: list[dict[str, Any]] = []
        self._dirty = True            # the device list must be (re)registered
        self._busy_until = 0.0
        self.last_error = ""
        self.last_ok = 0.0
        self.transport: Any = None    # tests plug in a fake mailbox here

    # -- config ------------------------------------------------------------

    @property
    def cfg(self) -> dict[str, Any]:
        c = self.access.data.setdefault("relay", {})
        c.setdefault("enabled", True)
        c.setdefault("url", DEFAULT_URL)
        if not c.get("box"):
            c["box"] = secrets.token_hex(16)
            c["pc_key"] = secrets.token_urlsafe(32)
            c["cursor"] = 0
            self.access.save()
        return c

    @property
    def enabled(self) -> bool:
        return available() and bool(self.cfg.get("enabled")) and bool(self.cfg.get("url"))

    def base(self) -> str:
        return str(self.cfg["url"]).rstrip("/") + API + "/" + self.cfg["box"]

    def devices(self) -> list[dict[str, Any]]:
        return [d for d in self.access.data.get("devices") or [] if d.get("relay_key") and d.get("relay_token_sha256")]

    def credentials(self, device: dict[str, Any]) -> dict[str, Any] | None:
        """New mailbox credentials for a paired phone (shown once; the PC keeps the token's hash)."""
        if not self.enabled:
            return None
        token = secrets.token_urlsafe(32)
        key = os.urandom(32)
        device["relay_token_sha256"] = sha256(token)
        device["relay_key"] = base64.b64encode(key).decode()
        self.access.save()
        self.changed()
        return {"url": str(self.cfg["url"]).rstrip("/"), "box": self.cfg["box"], "device_id": device["id"],
                "token": token, "key": device["relay_key"]}

    def changed(self) -> None:
        """Devices were added or removed: tell the mailbox who may read it."""
        self._dirty = True
        self._kick()

    # -- sending -----------------------------------------------------------

    def notify(self, ev: dict[str, Any]) -> None:
        """Pass a phone notification on to every phone, for when they are away. Thread-safe, never blocks."""
        if not self.enabled or not self.devices():
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._queue, {"t": "notice", "ev": ev})

    def _queue(self, msg: dict[str, Any]) -> None:
        self._outbox.append(msg)
        self._busy_until = time.time() + BUSY_FOR_S
        self._kick()

    def push_status(self) -> None:
        self.notify_raw({"t": "status"})

    def notify_raw(self, msg: dict[str, Any]) -> None:
        if not self.enabled or not self.devices() or self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._queue, msg)

    def _kick(self) -> None:
        if self._wake is not None and self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._wake.set)

    def _payload(self, msg: dict[str, Any]) -> dict[str, Any]:
        out = {**msg, "ts": time.time(), "pc_id": self.access.data.get("pc_id"), "pc_name": self.access.data.get("pc_name")}
        with contextlib.suppress(Exception):
            out["status"] = self.status()
        ev = out.get("ev")
        if isinstance(ev, dict) and ev.get("posts"):      # show the cover in the notification
            ev = {**ev, "posts": [{**p, "cover_jpg": cover_jpeg(p.get("cover"))} for p in ev["posts"][:3]]
                  + list(ev["posts"][3:])}
            out["ev"] = ev
        return out

    async def _flush(self, client: httpx.AsyncClient) -> None:
        if not self._outbox:
            return
        batch, self._outbox = self._outbox[:10], self._outbox[10:]
        items = []
        box = self.cfg["box"]
        for msg in batch:
            payload = await asyncio.to_thread(self._payload, msg)
            for d in self.devices():
                key = base64.b64decode(d["relay_key"])
                items.append({"to": d["id"], "body": seal(key, _aad(box, d["id"], "out"), payload)})
        if not items:
            return
        try:
            r = await client.post(self.base() + "/out", json={"items": items}, headers=self._auth())
            r.raise_for_status()
        except Exception:
            self._outbox[:0] = batch      # try again on the next round
            raise

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg['pc_key']}"}

    async def _register(self, client: httpx.AsyncClient) -> None:
        devs = [{"id": d["id"], "token_sha256": d["relay_token_sha256"]} for d in self.devices()]
        r = await client.post(self.base() + "/register", json={"devices": devs}, headers=self._auth())
        if r.status_code == 403:
            raise RuntimeError("中转信箱拒绝了这台电脑的密钥")
        r.raise_for_status()
        self._dirty = False
        if not self.cfg.get("registered"):
            self.cfg["registered"] = True
            self.access.save()

    # -- receiving ---------------------------------------------------------

    async def _poll(self, client: httpx.AsyncClient) -> None:
        cursor = int(self.cfg.get("cursor") or 0)
        r = await client.get(self.base() + "/in", params={"after": cursor}, headers=self._auth())
        r.raise_for_status()
        items = (r.json() or {}).get("items") or []
        by_id = {d["id"]: d for d in self.devices()}
        box = self.cfg["box"]
        for it in items:
            cursor = max(cursor, int(it.get("seq") or 0))
            dev = by_id.get(str(it.get("from") or ""))
            if not dev:
                continue            # a phone that has been disconnected since
            try:
                msg = unseal(base64.b64decode(dev["relay_key"]), _aad(box, dev["id"], "in"), str(it.get("body") or ""))
            except Exception:  # noqa: BLE001 - not sealed with this phone's key: ignore it
                log.warning("relay: dropped a message that does not open with %s's key", dev.get("name"))
                continue
            if time.time() - float(msg.get("ts") or 0) > 7 * 86400:
                continue
            if self.on_command:
                try:
                    await self.on_command(dev, msg)
                except Exception:  # noqa: BLE001
                    log.warning("relay command failed", exc_info=True)
        if cursor != int(self.cfg.get("cursor") or 0):
            self.cfg["cursor"] = cursor
            self.access.save()
        if items:
            self._busy_until = time.time() + BUSY_FOR_S

    # -- loop --------------------------------------------------------------

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._task = self._loop.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    def busy(self) -> bool:
        return time.time() < self._busy_until

    async def _run(self) -> None:
        fails = 0
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": "AutoStudio-PC"}, transport=self.transport) as client:
            while True:
                wait = POLL_BUSY_S if self.busy() else POLL_IDLE_S
                # the last phone being disconnected must still reach the mailbox
                if self.enabled and (self.devices() or (self._dirty and self.cfg.get("registered"))):
                    try:
                        if self._dirty:
                            await self._register(client)
                        if self.devices():
                            await self._flush(client)
                            await self._poll(client)
                        fails, self.last_error, self.last_ok = 0, "", time.time()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001 - offline, or the website is down: try later
                        fails += 1
                        self.last_error = str(e)[:200] or type(e).__name__
                        log.info("relay: %s", self.last_error)
                        wait = min(POLL_IDLE_S, 15 * 2 ** min(fails, 4))
                assert self._wake is not None
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), wait)

    def view(self) -> dict[str, Any]:
        return {"available": available(), "enabled": self.enabled, "url": self.cfg.get("url"),
                "phones": len(self.devices()), "last_ok": self.last_ok or None, "error": self.last_error}
