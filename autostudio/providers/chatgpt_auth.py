"""Sign in with ChatGPT (the same OAuth + PKCE flow the open-source Codex CLI uses).

Tokens are stored locally in ~/.autostudio/chatgpt_auth.json and only ever sent to OpenAI.
The ChatGPT/Codex backend is not a documented public API; it may change without notice.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx

from ..config import CHATGPT_AUTH_FILE

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
CALLBACK_PORT = 1455
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/auth/callback"
SCOPE = "openid profile email offline_access"
AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def decode_jwt(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


SUCCESS_HTML = """<!doctype html><meta charset="utf-8"><title>登录成功</title>
<body style="font-family:system-ui;background:#0f1115;color:#e8e8ea;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center"><div style="font-size:48px">✓</div><h2>ChatGPT 登录成功</h2>
<p style="opacity:.7">可以关闭此页面，回到 AutoStudio。</p></div>
<script>setTimeout(()=>window.close(),1500)</script></body>"""

FAIL_HTML = """<!doctype html><meta charset="utf-8"><title>登录失败</title>
<body style="font-family:system-ui;background:#0f1115;color:#e8e8ea;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center"><h2>登录失败</h2><p style="opacity:.7">{msg}</p></div></body>"""


class ChatGPTAuth:
    def __init__(self, path: Path = CHATGPT_AUTH_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        self._server: HTTPServer | None = None
        self.last_error: str = ""

    # ---- storage ------------------------------------------------------
    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text("utf-8"))
        except Exception:
            return None

    def _save(self, tokens: dict[str, Any]) -> dict[str, Any]:
        id_claims = decode_jwt(tokens.get("id_token", ""))
        access_claims = decode_jwt(tokens.get("access_token", ""))
        auth = id_claims.get(AUTH_CLAIM) or access_claims.get(AUTH_CLAIM) or {}
        data = {
            "id_token": tokens.get("id_token", ""),
            "access_token": tokens.get("access_token", ""),
            "refresh_token": tokens.get("refresh_token", ""),
            "account_id": tokens.get("account_id") or auth.get("chatgpt_account_id", ""),
            "email": id_claims.get("email") or (access_claims.get(PROFILE_CLAIM) or {}).get("email", ""),
            "plan": auth.get("chatgpt_plan_type", ""),
            "expires_at": access_claims.get("exp") or (time.time() + float(tokens.get("expires_in") or 3600)),
            "last_refresh": time.time(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), "utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return data

    def status(self) -> dict[str, Any]:
        d = self.load()
        if not d or not d.get("refresh_token"):
            return {"logged_in": False, "pending": self._pending is not None, "error": self.last_error}
        return {"logged_in": True, "email": d.get("email"), "plan": d.get("plan"), "account_id": d.get("account_id"),
                "expires_at": d.get("expires_at"), "pending": False, "error": ""}

    def logout(self) -> None:
        if self.path.exists():
            self.path.unlink()

    # ---- login flow ---------------------------------------------------
    def start_login(self) -> str:
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = _b64url(secrets.token_bytes(24))
        self._pending = {"verifier": verifier, "state": state, "started": time.time()}
        self.last_error = ""
        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": "codex_cli_rs",
        }
        self._start_callback_server()
        return f"{ISSUER}/oauth/authorize?{urllib.parse.urlencode(params)}"

    def _start_callback_server(self) -> None:
        self._stop_server()
        auth = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # silence
                pass

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/auth/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                ok, msg = auth.complete_from_query(parsed.query)
                body = (SUCCESS_HTML if ok else FAIL_HTML.format(msg=msg)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                if ok:
                    threading.Thread(target=auth._stop_server, daemon=True).start()

        try:
            server = HTTPServer(("127.0.0.1", CALLBACK_PORT), Handler)
        except OSError:
            self.last_error = f"端口 {CALLBACK_PORT} 被占用（可能 Codex CLI 正在登录）。可使用“手动粘贴回调地址”方式。"
            return
        server.timeout = 1
        self._server = server

        def serve() -> None:
            deadline = time.time() + 600
            while self._server is server and time.time() < deadline:
                try:
                    server.handle_request()
                except Exception:  # socket closed by _stop_server
                    break
            if self._server is server:
                self._stop_server()

        threading.Thread(target=serve, daemon=True, name="chatgpt-oauth-callback").start()

    def _stop_server(self) -> None:
        srv, self._server = self._server, None
        if srv:
            try:
                srv.server_close()
            except Exception:
                pass

    def complete_from_url(self, url: str) -> tuple[bool, str]:
        """Manual fallback: user pastes the full redirect URL (useful when running on a remote machine)."""
        parsed = urllib.parse.urlparse(url.strip())
        return self.complete_from_query(parsed.query)

    def complete_from_query(self, query: str) -> tuple[bool, str]:
        qs = urllib.parse.parse_qs(query)
        with self._lock:
            pending = self._pending
            if not pending:
                return False, "没有进行中的登录，请重新点击登录。"
            if qs.get("error"):
                self.last_error = qs.get("error_description", qs["error"])[0]
                return False, self.last_error
            if qs.get("state", [""])[0] != pending["state"]:
                self.last_error = "state 不匹配，请重新登录。"
                return False, self.last_error
            code = qs.get("code", [""])[0]
            if not code:
                return False, "缺少授权码。"
            try:
                r = httpx.post(
                    f"{ISSUER}/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": REDIRECT_URI,
                        "client_id": CLIENT_ID,
                        "code_verifier": pending["verifier"],
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=30,
                )
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
                self._save(r.json())
            except Exception as e:  # noqa: BLE001
                self.last_error = f"换取令牌失败：{e}"
                return False, self.last_error
            self._pending = None
            self.last_error = ""
            return True, ""

    def import_codex(self) -> dict[str, Any]:
        """Reuse an existing Codex CLI login (~/.codex/auth.json)."""
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        f = home / "auth.json"
        if not f.exists():
            raise FileNotFoundError(f"未找到 {f}，请先安装并登录 Codex CLI（codex login）。")
        data = json.loads(f.read_text("utf-8"))
        tokens = data.get("tokens") or {}
        if not tokens.get("refresh_token"):
            raise ValueError("Codex 登录信息中没有 ChatGPT 令牌（可能是 API Key 模式登录）。")
        self._save(tokens)
        return self.status()

    # ---- token use ----------------------------------------------------
    async def refresh(self) -> dict[str, Any]:
        d = self.load()
        if not d or not d.get("refresh_token"):
            raise PermissionError("尚未登录 ChatGPT。请在 设置 → 模型 中登录。")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{ISSUER}/oauth/token",
                json={"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": d["refresh_token"],
                      "scope": "openid profile email"},
            )
        if r.status_code >= 400:
            raise PermissionError(f"ChatGPT 令牌刷新失败 (HTTP {r.status_code})，请重新登录。{r.text[:200]}")
        new = r.json()
        merged = {
            "id_token": new.get("id_token") or d.get("id_token"),
            "access_token": new.get("access_token") or d.get("access_token"),
            "refresh_token": new.get("refresh_token") or d.get("refresh_token"),
            "account_id": d.get("account_id"),
            "expires_in": new.get("expires_in"),
        }
        return self._save(merged)

    async def credentials(self, force_refresh: bool = False) -> tuple[str, str]:
        d = self.load()
        if not d or not d.get("refresh_token"):
            raise PermissionError("尚未登录 ChatGPT。请在 设置 → 模型 中登录。")
        if force_refresh or float(d.get("expires_at") or 0) - time.time() < 300:
            d = await self.refresh()
        return d["access_token"], d.get("account_id", "")


chatgpt_auth = ChatGPTAuth()
