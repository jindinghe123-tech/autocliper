"""The phone relay: sealed messages, credentials handed out at pairing, approving a post from away from home."""
import asyncio
import base64
import hashlib
import json
import os
import tempfile
import time

os.environ.setdefault("AUTOSTUDIO_HOME", tempfile.mkdtemp(prefix="autostudio_test_"))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from autostudio import relay as relay_mod  # noqa: E402
from autostudio import remote as remote_mod  # noqa: E402
from autostudio import social, state  # noqa: E402
from autostudio.remote import RemoteAccess, _Tag  # noqa: E402
from autostudio.server import _relay_command, app  # noqa: E402

pytestmark = pytest.mark.skipif(not relay_mod.available(), reason="cryptography not installed")


class FakeMailbox:
    """The relay worker's protocol (relay/worker.js), in memory."""

    def __init__(self):
        self.pc_hash = None
        self.devices: dict[str, str] = {}
        self.msgs: list[dict] = []
        self.seq = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("authorization", "")[7:]
        what = req.url.path.rsplit("/", 1)[1]
        h = hashlib.sha256(auth.encode()).hexdigest()
        body = json.loads(req.content or b"{}") if req.method == "POST" else {}
        if what == "register":
            if self.pc_hash and self.pc_hash != h:
                return httpx.Response(403)
            self.pc_hash = h
            self.devices = {d["id"]: d["token_sha256"] for d in body["devices"]}
            return httpx.Response(200, json={"ok": True})
        pc = h == self.pc_hash
        if what == "out" and req.method == "POST" and pc:
            for it in body["items"]:
                self.seq += 1
                self.msgs.append({"seq": self.seq, "dir": "out", "peer": it["to"], "body": it["body"]})
            return httpx.Response(200, json={"ok": True})
        if what == "in" and req.method == "GET" and pc:
            after = int(req.url.params.get("after") or 0)
            self.msgs = [m for m in self.msgs if not (m["dir"] == "in" and m["seq"] <= after)]
            return httpx.Response(200, json={"items": [{"seq": m["seq"], "from": m["peer"], "body": m["body"]}
                                                        for m in self.msgs if m["dir"] == "in" and m["seq"] > after]})
        return httpx.Response(403)

    def phone_inbox(self, device_id: str) -> list[dict]:
        return [m for m in self.msgs if m["dir"] == "out" and m["peer"] == device_id]

    def phone_says(self, device_id: str, key: bytes, box: str, msg: dict) -> None:
        self.seq += 1
        body = relay_mod.seal(key, relay_mod._aad(box, device_id, "in"), {**msg, "ts": time.time()})
        self.msgs.append({"seq": self.seq, "dir": "in", "peer": device_id, "body": body})


@pytest.fixture()
def ra(tmp_path, monkeypatch):
    r = RemoteAccess(tmp_path / "remote.json")
    monkeypatch.setattr(remote_mod, "remote", r)
    monkeypatch.setattr("autostudio.server.remote", r)
    return r


def test_seal_is_bound_to_key_and_direction():
    key = os.urandom(32)
    aad = relay_mod._aad("b" * 32, "dev1", "out")
    body = relay_mod.seal(key, aad, {"t": "notice", "text": "厨房做好了"})
    assert "厨房" not in body and relay_mod.unseal(key, aad, body)["text"] == "厨房做好了"
    for bad_key, bad_aad in ((os.urandom(32), aad), (key, relay_mod._aad("b" * 32, "dev1", "in")),
                             (key, relay_mod._aad("b" * 32, "dev2", "out"))):
        with pytest.raises(Exception):
            relay_mod.unseal(bad_key, bad_aad, body)


def test_pairing_hands_out_relay_credentials(ra):
    ra.data["enabled"] = True
    code = ra.pairing_code()["code"]
    phone = TestClient(_Tag(app), base_url="http://192.168.1.20:8766")
    r = phone.post("/api/remote/pair", headers={"X-AutoStudio": "1"}, json={"code": code, "name": "爸爸的手机", "platform": "android"})
    assert r.status_code == 200, r.text
    d = r.json()
    creds = d["relay"]
    assert creds["box"] == ra.data["relay"]["box"] and len(base64.b64decode(creds["key"])) == 32
    dev = ra.data["devices"][0]
    assert dev["relay_token_sha256"] == hashlib.sha256(creds["token"].encode()).hexdigest()
    assert "relay_key" not in json.dumps(ra.status())        # never shown in the PC's device list
    # later, on the local network, the phone can get fresh ones (the old token stops working)
    again = phone.get("/api/remote/relay", headers={"Authorization": f"Bearer {d['token']}"}).json()
    assert again["token"] != creds["token"] and dev["relay_token_sha256"] == hashlib.sha256(again["token"].encode()).hexdigest()
    stranger = TestClient(_Tag(app), base_url="http://192.168.1.21:8766")
    assert stranger.get("/api/remote/relay").status_code == 401     # not without being paired


def test_approve_from_away_publishes(ra, tmp_path, monkeypatch):
    mailbox = FakeMailbox()
    ra.relay.transport = httpx.MockTransport(mailbox.handler)
    ra.relay.on_command = _relay_command
    ra.relay.status = lambda: {"active": 0, "next_task_in": None, "pending_ids": []}
    acc = state.db.create_account("xiaohongshu", "装修号")
    video = tmp_path / "成片.mp4"
    video.write_bytes(b"v")
    session = state.db.create_session(title="⏰ 每日装修视频", kind="task")
    post = state.db.create_post(acc["id"], "xiaohongshu", {"video": str(video), "title": "厨房翻新"}, session_id=session["id"])
    published = []

    async def fake_xhs(acc, content, progress, **kw):
        published.append(content["title"])
        return {"url": "https://creator.xiaohongshu.com/new/note-manager"}

    monkeypatch.setattr(social, "_run_xhs", fake_xhs)

    async def main():
        dev, _ = ra.pair(ra.pairing_code()["code"], "爸爸的手机", "android", "1.2.3.4")
        creds = ra.relay.credentials(dev)
        key, box = base64.b64decode(creds["key"]), creds["box"]
        ra.relay.start()
        social.announce_pending(session["id"])          # the run queued a post: the phone is told, wherever it is
        for _ in range(100):
            if mailbox.phone_inbox(dev["id"]):
                break
            await asyncio.sleep(0.02)
        msg = relay_mod.unseal(key, relay_mod._aad(box, dev["id"], "out"), mailbox.phone_inbox(dev["id"])[0]["body"])
        assert msg["ev"]["type"] == "posts_pending" and msg["ev"]["posts"][0]["title"] == "厨房翻新"
        mailbox.phone_says(dev["id"], key, box, {"t": "approve", "post": post["id"]})     # 同意, on mobile data
        ra.relay._kick()
        for _ in range(200):
            if published and state.db.get_post(post["id"])["status"] == "published":
                break
            await asyncio.sleep(0.02)
        # how it went reaches the phone the same way
        for _ in range(100):
            kinds = [relay_mod.unseal(key, relay_mod._aad(box, dev["id"], "out"), m["body"])["ev"]["type"]
                     for m in mailbox.phone_inbox(dev["id"])]
            if "post_published" in kinds:
                break
            ra.relay._kick()
            await asyncio.sleep(0.02)
        await ra.relay.stop()
        return kinds

    kinds = asyncio.run(main())
    assert published == ["厨房翻新"] and state.db.get_post(post["id"])["status"] == "published"
    assert "post_published" in kinds
    state.db.delete_account(acc["id"])


def test_events_list_whats_waiting(ra, tmp_path):
    acc = state.db.create_account("xiaohongshu", "装修号2")
    post = state.db.create_post(acc["id"], "xiaohongshu", {"video": "x.mp4", "title": "卫生间", "cover": "c.jpg",
                                                          "description": "防水做了三遍", "tags": ["装修"]})
    dev, token = ra.pair(ra.pairing_code()["code"], "手机", "android", "1.2.3.4")
    phone = TestClient(_Tag(app), base_url="http://192.168.1.20:8766")
    d = phone.get("/api/remote/events", headers={"Authorization": f"Bearer {token}"}).json()
    mine = [p for p in d["pending"] if p["id"] == post["id"]]
    assert mine and mine[0]["title"] == "卫生间" and mine[0]["account"] == "小红书「装修号2」" and mine[0]["cover"] == "c.jpg"
    assert post["id"] in d["pending_ids"]
    state.db.delete_post(post["id"])
    state.db.delete_account(acc["id"])


@pytest.mark.skipif(not os.environ.get("AUTOSTUDIO_RELAY_URL"), reason="set AUTOSTUDIO_RELAY_URL to test a running relay worker")
def test_live_worker(ra):
    """Against `npx wrangler dev` (or the deployed site): AUTOSTUDIO_RELAY_URL=http://127.0.0.1:8787"""
    url = os.environ["AUTOSTUDIO_RELAY_URL"].rstrip("/")
    ra.data["relay"] = {"enabled": True, "url": url}
    heard = []

    async def on_command(dev, msg):
        heard.append(msg)

    async def main():
        ra.relay.on_command = on_command
        dev, _ = ra.pair(ra.pairing_code()["code"], "手机", "android", "1.2.3.4")
        creds = ra.relay.credentials(dev)
        key, box = base64.b64decode(creds["key"]), creds["box"]
        ra.relay.start()
        ra.notices.publish({"type": "run_done", "session_id": "s", "title": "t", "text": "做好了"})
        async with httpx.AsyncClient(timeout=10) as c:
            items = []
            for _ in range(60):
                items = (await c.get(f"{url}/relay/v1/{box}/out", params={"device": dev["id"]},
                                     headers={"Authorization": f"Bearer {creds['token']}"})).json().get("items") or []
                if items:
                    break
                await asyncio.sleep(0.25)
            assert relay_mod.unseal(key, relay_mod._aad(box, dev["id"], "out"), items[0]["body"])["ev"]["text"] == "做好了"
            body = relay_mod.seal(key, relay_mod._aad(box, dev["id"], "in"), {"t": "sync", "ts": time.time()})
            r = await c.post(f"{url}/relay/v1/{box}/in", json={"body": body},
                             headers={"Authorization": f"Bearer {creds['token']}", "X-Device": dev["id"]})
            assert r.status_code == 200
        ra.relay._busy_until = time.time() + 60
        ra.relay._kick()
        for _ in range(60):
            if heard:
                break
            await asyncio.sleep(0.25)
        await ra.relay.stop()

    asyncio.run(main())
    assert heard and heard[0]["t"] == "sync"
