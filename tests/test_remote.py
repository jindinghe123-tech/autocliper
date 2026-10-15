"""Phone access: pairing codes, device tokens and what the phone listener lets through."""
import os
import tempfile

os.environ.setdefault("AUTOSTUDIO_HOME", tempfile.mkdtemp(prefix="autostudio_test_"))

import pytest
from fastapi.testclient import TestClient

from autostudio import remote as remote_mod
from autostudio.remote import PairError, RemoteAccess, _Tag, device_label
from autostudio.server import app

H = {"X-AutoStudio": "1"}


@pytest.fixture()
def ra(tmp_path, monkeypatch):
    r = RemoteAccess(tmp_path / "remote.json")
    monkeypatch.setattr(remote_mod, "remote", r)
    monkeypatch.setattr("autostudio.server.remote", r)
    return r


def test_pairing_code_is_single_use_and_limited(ra):
    code = ra.pairing_code()["code"]
    assert len(code) == 6 and code.isdigit()
    with pytest.raises(PairError):
        ra.pair("000000" if code != "000000" else "111111", "x", "android", "1.2.3.4")
    dev, token = ra.pair(code, "我的手机", "android", "1.2.3.4")
    assert ra.authenticate(token)["id"] == dev["id"]
    assert token not in (tmp := ra.path.read_text("utf-8")) and dev["token_sha256"] in tmp   # only the hash is stored
    with pytest.raises(PairError, match="失效"):
        ra.pair(code, "again", "android", "1.2.3.4")
    wrong = "000000" if ra.pairing_code()["code"] != "000000" else "111111"
    for _ in range(remote_mod.CODE_ATTEMPTS - 1):
        with pytest.raises(PairError, match="不对"):
            ra.pair(wrong, "x", "web", "")
    with pytest.raises(PairError, match="作废"):
        ra.pair(wrong, "x", "web", "")
    assert not ra.pairing_open()


def test_revoke_and_reload(ra, tmp_path):
    code = ra.pairing_code()["code"]
    dev, token = ra.pair(code, "phone", "android", "")
    assert RemoteAccess(tmp_path / "remote.json").authenticate(token)
    assert ra.revoke(dev["id"])
    assert ra.authenticate(token) is None
    assert RemoteAccess(tmp_path / "remote.json").authenticate(token) is None


def test_device_label():
    assert device_label("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)") == "iPhone"
    assert device_label("Mozilla/5.0 (Linux; Android 14; 23078RKD5C Build/UP1A) AppleWebKit") == "安卓手机 23078RKD5C"
    assert device_label("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit") == "安卓手机"


def test_phone_listener_requires_pairing(ra):
    phone = TestClient(_Tag(app), base_url="http://192.168.1.20:8766")
    assert phone.get("/api/sessions").status_code == 401
    assert phone.get("/api/sessions").json()["code"] == "unpaired"
    r = phone.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pair"
    assert phone.get("/pair").status_code == 200
    assert phone.get("/api/remote/hello").json()["app"] == "AutoStudio"

    code = ra.pairing_code()["code"]
    r = phone.post("/api/remote/pair", json={"code": code, "platform": "android"}, headers=H)
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert "as_device" in phone.cookies

    assert phone.get("/api/sessions").status_code == 200                    # cookie
    fresh = TestClient(_Tag(app), base_url="http://192.168.1.20:8766")
    assert fresh.get("/api/sessions", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert phone.get("/api/meta").json()["remote"]["pc_name"] == ra.data["pc_name"]
    # remote-access settings stay on the PC
    assert phone.get("/api/remote").status_code == 403
    assert phone.put("/api/remote", json={"enabled": False}, headers=H).status_code == 403
    assert phone.get("/api/remote/qr.svg").status_code == 403
    assert phone.get("/api/remote/whoami").json()["device"]["platform"] == "android"
    # mutating calls still need the anti-CSRF header
    assert phone.post("/api/remote/logout").status_code == 403
    assert phone.post("/api/remote/logout", headers=H).status_code == 200
    assert fresh.get("/api/sessions", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_pc_listener_unchanged(ra):
    pc = TestClient(app, base_url="http://127.0.0.1:8765")
    assert pc.get("/api/remote").status_code == 200
    assert pc.get("/api/meta").json()["remote"] is None
    # pairing only happens through the phone listener
    assert pc.post("/api/remote/pair", json={"code": ra.pairing_code()["code"]}, headers=H).status_code == 400
    evil = TestClient(app, base_url="http://evil.example:8765")
    assert evil.get("/api/sessions").status_code == 403
