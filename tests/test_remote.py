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


def _paired_phone(ra):
    phone = TestClient(_Tag(app), base_url="http://192.168.1.20:8766")
    r = phone.post("/api/remote/pair", json={"code": ra.pairing_code()["code"], "platform": "android"}, headers=H)
    token = r.json()["token"]
    return TestClient(_Tag(app), base_url="http://192.168.1.20:8766"), {"Authorization": f"Bearer {token}"}, r.json()


def test_events_need_a_paired_phone(ra):
    stranger = TestClient(_Tag(app), base_url="http://192.168.1.20:8766")
    r = stranger.get("/api/remote/events")
    assert r.status_code == 401 and r.json()["code"] == "unpaired"
    assert stranger.get("/api/remote/events", headers={"Authorization": "Bearer nope"}).status_code == 401

    phone, auth, paired = _paired_phone(ra)
    r = phone.get("/api/remote/events", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["boot"] == ra.notices.boot and r.json()["events"] == []

    ra.revoke(paired["device"]["id"])
    assert phone.get("/api/remote/events", headers=auth).status_code == 401


def test_events_feed_cursor_and_long_poll(ra):
    import threading
    import time

    phone, auth, _ = _paired_phone(ra)
    ra.notices.publish({"type": "run_done", "session_id": "old"})
    first = phone.get("/api/remote/events", headers=auth).json()      # first contact: no backlog
    assert first["events"] == [] and first["cursor"] == 1
    q = {"after": first["cursor"], "boot": first["boot"]}
    assert phone.get("/api/remote/events", params=q, headers=auth).json()["events"] == []

    threading.Timer(0.3, ra.notices.publish, [{"type": "approval_needed", "session_id": "s1"}]).start()
    t0 = time.monotonic()
    r = phone.get("/api/remote/events", params={**q, "wait": 20}, headers=auth).json()
    assert time.monotonic() - t0 < 10
    assert [e["session_id"] for e in r["events"]] == ["s1"] and r["cursor"] == 2

    # a cursor from before the PC app restarted: read this run's whole feed
    stale = phone.get("/api/remote/events", params={"after": 5, "boot": "previous"}, headers=auth).json()
    assert [e["id"] for e in stale["events"]] == [1, 2]


def test_runs_notify_done_error_and_approvals():
    import asyncio

    from autostudio.agent.runs import RunManager

    heard = []

    async def main():
        rm = RunManager(notify=lambda sid, ev: heard.append((sid, ev["type"])))

        async def ok(run):
            assert await run.request_approval("a1", "run_command", {}, timeout=0.05) is False
            return "视频做好了"

        async def boom(run):
            raise RuntimeError("坏了")

        async def slow(run):
            await asyncio.sleep(10)

        for sid, fn in (("s1", ok), ("s2", boom), ("s3", slow)):
            rm.start(sid, fn)
        await asyncio.sleep(0.01)
        rm.stop("s3")
        for sid in ("s1", "s2", "s3"):
            await rm.wait(sid)

    asyncio.run(main())
    assert ("s1", "approval_needed") in heard and ("s1", "approval_resolved") in heard
    assert ("s1", "run_done") in heard and ("s2", "run_error") in heard
    assert not [h for h in heard if h[0] == "s3"]      # stopping a run yourself is not news


def test_run_notice_skips_quick_chats(ra):
    from autostudio import state
    from autostudio.server import _run_notice

    chat = state.db.create_session(title="做个视频")
    task = state.db.create_session(title="⏰ 每日视频", kind="task")
    _run_notice(chat["id"], {"type": "run_done", "text": "好的", "seconds": 3})
    _run_notice(chat["id"], {"type": "approval_needed", "name": "run_command", "seconds": 3})
    _run_notice(task["id"], {"type": "run_done", "text": "**做好了** [看看](out.mp4)", "seconds": 3})
    _run_notice(chat["id"], {"type": "run_error", "text": "模型\n出错", "seconds": 120})
    got = list(ra.notices.items)
    assert [(e["type"], e["session_id"]) for e in got] == [
        ("approval_needed", chat["id"]), ("run_done", task["id"]), ("run_error", chat["id"])]
    assert got[0]["label"] == "执行命令" and got[0]["title"] == "做个视频"
    assert got[1]["task"] is True and got[1]["text"] == "做好了 看看"
    assert got[2]["text"] == "模型 出错"
