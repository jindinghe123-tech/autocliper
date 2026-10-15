"""Phone access: pair phones with this PC and let them use the full AutoStudio UI over the network.

The desktop UI only listens on 127.0.0.1. Turning on phone access opens a second listener on all
interfaces (default port 8766) that serves the very same app, so the phone gets the same pages and
everything still runs on this PC. Everything on that listener except the pairing page needs the token
of a paired device, and pairing needs the 6-digit code that is shown only on this PC's screen: a phone
can do nothing until someone deliberately connects it from the PC.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import platform
import secrets
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import DATA_DIR

log = logging.getLogger("autostudio.remote")

REMOTE_FILE = DATA_DIR / "remote.json"
APK_FILE = Path(__file__).parent / "app" / "AutoStudio.apk"
DEFAULT_PORT = 8766
COOKIE = "as_device"
COOKIE_MAX_AGE = 400 * 86400      # browsers cap cookie lifetime at 400 days
CODE_TTL = 600                    # a pairing code lives 10 minutes
CODE_ATTEMPTS = 8                 # wrong guesses before the code is thrown away
SEEN_SAVE_EVERY = 60

# Reachable from a phone that is not paired yet: the pairing page and what it needs.
PUBLIC_PATHS = {"/pair", "/api/remote/hello", "/api/remote/pair", "/download/AutoStudio.apk",
                "/manifest.webmanifest", "/favicon.ico"}
# Remote-management endpoints a paired phone may use (everything else under /api/remote is PC-only).
DEVICE_PATHS = {"/api/remote/whoami", "/api/remote/logout"}
CGNAT = ipaddress.ip_network("100.64.0.0/10")   # Tailscale / ZeroTier style overlay networks


class PairError(Exception):
    pass


def _quiet_server(config: Any) -> Any:
    """A uvicorn.Server that leaves signal handling to the main server (both run in the same loop)."""
    import uvicorn

    class Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield

    return Server(config)


class _Tag:
    """ASGI wrapper: marks every request that came in through the phone listener."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "autostudio_remote": True}
        await self.app(scope, receive, send)


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":     # on Windows SO_REUSEADDR would let two processes share the port
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def local_addresses() -> list[dict[str, str]]:
    """IPv4 addresses a phone could use to reach this PC, the most likely one first."""
    found: list[str] = []
    with contextlib.suppress(OSError):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))   # UDP connect sends nothing; it just picks the outgoing interface
            found.append(s.getsockname()[0])
        finally:
            s.close()
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(str(info[4][0]))
    out: list[dict[str, str]] = []
    for i, ip in enumerate(dict.fromkeys(found)):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.is_loopback or a.is_link_local or a.is_unspecified:
            continue
        if a in CGNAT:
            label = "虚拟局域网（Tailscale 等，不在家也能连）"
        elif a.is_private:
            label = "局域网（推荐）" if i == 0 else "局域网"
        else:     # e.g. Radmin / Hamachi VPN adapters (26.x, 25.x)
            label = "其他网络（VPN 等）"
        out.append({"ip": ip, "label": label})
    return out


def device_label(ua: str) -> str:
    """A readable default name for a browser that pairs without telling us its name."""
    ua = ua or ""
    if "iPhone" in ua:
        return "iPhone"
    if "iPad" in ua:
        return "iPad"
    if "Android" in ua:
        import re

        m = re.search(r"Android [\d.]+; ([^;)]+?)(?: Build/[^;)]*)?\)", ua)
        model = (m.group(1).strip() if m else "") if m and m.group(1).strip() not in ("K", "wv") else ""
        return f"安卓手机 {model}".strip()
    if "Mac OS X" in ua:
        return "Mac 浏览器"
    if "Windows" in ua:
        return "Windows 浏览器"
    return "浏览器"


class RemoteAccess:
    def __init__(self, path: Path = REMOTE_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.data = self._load()
        self._code: dict[str, Any] | None = None
        self._server: Any = None
        self._task: asyncio.Task | None = None
        self._app: Any = None
        self._last_save = 0.0
        self.error = ""

    # -- storage ------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text("utf-8"))
            if not isinstance(d, dict):
                d = {}
        except (OSError, ValueError):
            d = {}
        d.setdefault("pc_id", uuid.uuid4().hex[:16])
        d.setdefault("pc_name", (platform.node() or "我的电脑")[:40])
        d.setdefault("enabled", False)
        d.setdefault("port", DEFAULT_PORT)
        d["devices"] = [x for x in d.get("devices") or [] if isinstance(x, dict) and x.get("token_sha256")]
        return d

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(self.path)
            self._last_save = time.time()

    # -- devices ------------------------------------------------------------

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        digest = self._digest(token)
        for d in self.data["devices"]:
            if hmac.compare_digest(d["token_sha256"], digest):
                return d
        return None

    def touch(self, device: dict[str, Any], ip: str) -> None:
        device["last_seen"] = time.time()
        device["last_ip"] = ip
        if time.time() - self._last_save > SEEN_SAVE_EVERY:
            self.save()

    def device_view(self, d: dict[str, Any]) -> dict[str, Any]:
        return {k: d.get(k) for k in ("id", "name", "platform", "created", "last_seen", "last_ip")}

    def revoke(self, device_id: str) -> bool:
        before = len(self.data["devices"])
        self.data["devices"] = [d for d in self.data["devices"] if d["id"] != device_id]
        if len(self.data["devices"]) != before:
            self.save()
            return True
        return False

    def rename(self, device_id: str, name: str) -> dict[str, Any] | None:
        for d in self.data["devices"]:
            if d["id"] == device_id:
                d["name"] = name[:40]
                self.save()
                return d
        return None

    # -- pairing ------------------------------------------------------------

    def pairing_code(self, refresh: bool = False) -> dict[str, Any]:
        c = self._code
        if refresh or not c or c["expires"] < time.time():
            c = self._code = {"code": f"{secrets.randbelow(10 ** 6):06d}", "expires": time.time() + CODE_TTL, "attempts": 0}
        return {"code": c["code"], "expires_in": max(0, int(c["expires"] - time.time()))}

    def pairing_open(self) -> bool:
        return bool(self._code and self._code["expires"] > time.time())

    def pair(self, code: str, name: str, platform_: str, ip: str) -> tuple[dict[str, Any], str]:
        c = self._code
        if not c or c["expires"] < time.time():
            raise PairError("配对码已失效：在电脑上打开 AutoStudio 的「手机连接」页面，用上面显示的新配对码")
        if not hmac.compare_digest(c["code"], "".join(ch for ch in str(code) if ch.isdigit())):
            c["attempts"] += 1
            if c["attempts"] >= CODE_ATTEMPTS:
                self._code = None
                raise PairError("错误次数太多，这个配对码已作废。请看电脑上刷新后的新配对码")
            raise PairError("配对码不对，请核对电脑上显示的 6 位数字")
        token = secrets.token_urlsafe(32)
        dev = {"id": uuid.uuid4().hex[:12], "name": (name or "手机").strip()[:40] or "手机",
               "platform": (platform_ or "web")[:20], "token_sha256": self._digest(token),
               "created": time.time(), "last_seen": time.time(), "last_ip": ip}
        self.data["devices"].append(dev)
        self.save()
        self._code = None          # one code, one phone: the PC page shows a fresh code right away
        return dev, token

    # -- listener -----------------------------------------------------------

    @property
    def port(self) -> int:
        return int(self.data.get("port") or DEFAULT_PORT)

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done() and self._server and self._server.started)

    async def start(self, app: Any) -> None:
        await self.stop()
        self._app = app
        port = self.port
        try:
            sock = _bind(port)
        except OSError as e:
            self.error = f"端口 {port} 已被占用或无法监听（{e.strerror or e}）。换一个端口再试。"
            log.warning("phone listener: %s", self.error)
            return
        import uvicorn

        config = uvicorn.Config(_Tag(app), lifespan="off", log_config=None, log_level="warning", access_log=False,
                                proxy_headers=False, timeout_graceful_shutdown=2)
        self._server = _quiet_server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        self.error = ""
        for _ in range(50):
            if self._server.started or self._task.done():
                break
            await asyncio.sleep(0.02)
        if self._task.done() and not self._server.started:
            self.error = "手机连接服务没能启动"
            with contextlib.suppress(BaseException):
                self._task.result()

    async def stop(self) -> None:
        server, task = self._server, self._task
        self._server = self._task = None
        if server and task:
            server.should_exit = True
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 5)

    async def apply(self, app: Any) -> None:
        """Bring the listener in line with the saved settings."""
        if self.data.get("enabled"):
            if not self.running or self._bound_port != self.port:
                await self.start(app)
        else:
            await self.stop()
            self.error = ""

    @property
    def _bound_port(self) -> int | None:
        with contextlib.suppress(Exception):
            return self._server.servers[0].sockets[0].getsockname()[1]
        return None

    # -- views --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        port = self.port
        addrs = [{**a, "url": f"http://{a['ip']}:{port}"} for a in local_addresses()]
        return {"enabled": bool(self.data.get("enabled")), "running": self.running, "error": self.error,
                "port": port, "pc_id": self.data["pc_id"], "pc_name": self.data["pc_name"], "addresses": addrs,
                "devices": sorted((self.device_view(d) for d in self.data["devices"]),
                                  key=lambda d: -(d.get("last_seen") or 0)),
                "apk": APK_FILE.is_file()}


remote = RemoteAccess()


# ---------------------------------------------------------------------------
# Windows firewall: the most common reason a phone cannot reach the PC is that the "allow network
# access" prompt for Python was dismissed (Windows then blocks it) or only covers the other network type.

FIREWALL_RULE = "AutoStudio Phone Access"


def _process_image() -> str:
    """Path of the executable actually running (a venv's python.exe only launches the real interpreter)."""
    if sys.platform == "win32":
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf)):
            return buf.value
    return sys.executable


def _ps(script: str, timeout: float = 20) -> str:
    import base64
    import subprocess

    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                       check=False, capture_output=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace").strip()[-300:] or f"exit {r.returncode}")
    return r.stdout.decode("utf-8", "replace")


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def firewall_status() -> dict[str, Any]:
    """Would Windows Firewall let a phone on the current network reach this process?"""
    if sys.platform != "win32":
        return {"checked": False}
    exe = _process_image()
    script = f"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$exe = {_ps_quote(exe)}
$rules = @(Get-NetFirewallApplicationFilter | Where-Object {{ $_.Program -ieq $exe }} | Get-NetFirewallRule)
$rules += @(Get-NetFirewallRule -DisplayName {_ps_quote(FIREWALL_RULE)})
@{{
  profiles = @(Get-NetFirewallProfile | ForEach-Object {{ @{{ name = $_.Name; enabled = ([string]$_.Enabled -eq 'True') }} }})
  networks = @(Get-NetConnectionProfile | ForEach-Object {{ @{{ alias = $_.InterfaceAlias; cat = [string]$_.NetworkCategory }} }})
  rules = @($rules | Where-Object {{ $_ -and [string]$_.Direction -eq 'Inbound' }} | ForEach-Object {{
    @{{ name = $_.DisplayName; action = [string]$_.Action; profile = [string]$_.Profile; enabled = ([string]$_.Enabled -eq 'True') }} }})
}} | ConvertTo-Json -Depth 4 -Compress
"""
    try:
        d = json.loads(_ps(script) or "{}")
    except Exception as e:  # noqa: BLE001 — no PowerShell / policy restrictions: just don't show the check
        return {"checked": False, "error": str(e)}
    enabled = {p["name"]: p["enabled"] for p in d.get("profiles") or []}
    rules = [r for r in d.get("rules") or [] if r.get("enabled")]
    blocked: list[str] = []
    cats = {"DomainAuthenticated": "Domain"}
    for net in d.get("networks") or []:
        cat = cats.get(net.get("cat"), net.get("cat") or "Public")
        if not enabled.get(cat, True):
            continue
        covering = [r for r in rules if r.get("profile") == "Any" or cat in (r.get("profile") or "")]
        refused = any(r["action"] == "Block" for r in covering) or not any(r["action"] == "Allow" for r in covering)
        if refused and cat not in blocked:
            blocked.append(cat)
    names = {"Public": "公用网络", "Private": "专用网络", "Domain": "域网络"}
    return {"checked": True, "ok": not blocked, "blocked_on": [names.get(c, c) for c in blocked], "program": exe}


def firewall_fix(port: int) -> None:
    """Allow incoming connections to this program on the phone port (local network + Tailscale only).

    Needs administrator rights, so Windows shows its UAC prompt; the person at the PC decides.
    """
    if sys.platform != "win32":
        raise RuntimeError("只有 Windows 需要这一步")
    exe = _process_image()
    inner = f"""
$exe = {_ps_quote(exe)}
Get-NetFirewallApplicationFilter | Where-Object {{ $_.Program -ieq $exe }} | Get-NetFirewallRule |
  Where-Object {{ [string]$_.Direction -eq 'Inbound' -and [string]$_.Action -eq 'Block' }} | Disable-NetFirewallRule
Remove-NetFirewallRule -DisplayName {_ps_quote(FIREWALL_RULE)} -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName {_ps_quote(FIREWALL_RULE)} -Direction Inbound -Program $exe -Protocol TCP -LocalPort {int(port)} `
  -Action Allow -Profile Any -RemoteAddress LocalSubnet,100.64.0.0/10 | Out-Null
"""
    import base64

    enc = base64.b64encode(inner.encode("utf-16-le")).decode()
    _ps(f"$p = Start-Process powershell -Verb RunAs -WindowStyle Hidden -Wait -PassThru "
        f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','{enc}'; exit $p.ExitCode", timeout=180)
