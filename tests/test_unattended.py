"""Unattended publishing: a task's material folder, approval notifications on the phone, publish results."""
import asyncio
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("AUTOSTUDIO_HOME", tempfile.mkdtemp(prefix="autostudio_test_"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from autostudio import remote as remote_mod  # noqa: E402
from autostudio import scheduler as sched_mod  # noqa: E402
from autostudio import social, state  # noqa: E402
from autostudio.agent.runs import RunManager  # noqa: E402
from autostudio.remote import RemoteAccess, _Tag  # noqa: E402
from autostudio.server import _run_notice, app  # noqa: E402
from autostudio.tools.registry import ToolContext  # noqa: E402
from autostudio.tools.social import social_publish  # noqa: E402

H = {"X-AutoStudio": "1"}
PC = "http://127.0.0.1:8765"
OLD = time.time() - 3600


@pytest.fixture()
def ra(tmp_path, monkeypatch):
    r = RemoteAccess(tmp_path / "remote.json")
    monkeypatch.setattr(remote_mod, "remote", r)
    monkeypatch.setattr("autostudio.server.remote", r)
    return r


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(type(state.settings), "workspace", property(lambda self: ws))
    return ws


def put_file(folder: Path, name: str, data: bytes = b"x" * 2048, mtime: float = OLD) -> Path:
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))
    return p


# ---------------------------------------------------------------------------
# material folder

def test_scan_new_files(tmp_path):
    put_file(tmp_path, "b.mp4", mtime=OLD + 10)
    put_file(tmp_path, "a.jpg")
    put_file(tmp_path, "sub/c.mov", mtime=OLD + 20)
    put_file(tmp_path, "copying.mp4", mtime=time.time())             # still being copied in
    for junk in ("~$x.docx", ".hidden.mp4", "dl.crdownload", "Thumbs.db", ".git/obj"):
        put_file(tmp_path, junk)
    new = sched_mod.scan_new(tmp_path, {})
    assert [f["rel"] for f in new] == ["a.jpg", "b.mp4", "sub/c.mov"]      # oldest first, junk skipped
    seen = {f["rel"]: f["key"] for f in new}
    assert sched_mod.scan_new(tmp_path, seen) == []
    put_file(tmp_path, "a.jpg", b"edited" * 999)                            # re-exported: new again
    assert [f["rel"] for f in sched_mod.scan_new(tmp_path, seen)] == ["a.jpg"]


def _task(folder: Path, **kw):
    return state.db.create_task(name="素材剪辑", prompt="把新素材剪成短视频发小红书", cron="0 18 * * *",
                                watch_dir=str(folder), **kw)


def test_task_runs_only_on_new_material(ra, tmp_path, monkeypatch):
    folder = tmp_path / "素材"
    folder.mkdir()
    prompts: list[str] = []
    outcome = {"fail": False}

    async def fake_agent(run, sid, prompt, **kw):
        prompts.append(prompt)
        if outcome["fail"]:
            raise RuntimeError("模型挂了")
        return "做好了"

    monkeypatch.setattr("autostudio.agent.loop.run_agent", fake_agent)
    t = _task(folder)

    async def main():
        state.runs = RunManager(notify=_run_notice)
        sch = sched_mod.TaskScheduler()

        async def run():
            r = await sch.run_task(t["id"])
            if r and r.get("session_id"):
                await state.runs.wait(r["session_id"])
            return r

        r = await run()        # empty folder: nothing runs
        assert r["skipped"] and "没有新文件" in r["message"] and not prompts
        put_file(folder, "旅行.mp4")
        put_file(folder, "封面.jpg", mtime=OLD + 5)
        outcome["fail"] = True
        r = await run()        # the run fails: the files are not used up...
        assert r["session_id"] and "旅行.mp4" in prompts[-1] and "封面.jpg" in prompts[-1]
        outcome["fail"] = False
        r = await run()        # ...so the next run gets them again
        assert r["session_id"] and len(prompts) == 2 and str(folder / "旅行.mp4") in prompts[-1]
        r = await run()        # used: nothing new any more
        assert r["skipped"] and len(prompts) == 2
        put_file(folder, "第二天.mp4")
        r = await run()
        assert "第二天.mp4" in prompts[-1] and "旅行.mp4" not in prompts[-1]

    asyncio.run(main())
    runs = state.db.list_task_runs(t["id"])
    assert [r["status"] for r in reversed(runs)] == ["skipped", "error", "ok", "skipped", "ok"]
    assert not [e for e in ra.notices.items if e.get("route") == "#/tasks"]     # skipping is not news
    state.db.delete_task(t["id"])


def test_missing_folder_is_reported(ra, tmp_path):
    t = _task(tmp_path / "gone")

    async def main():
        state.runs = RunManager()
        return await sched_mod.TaskScheduler().run_task(t["id"])

    r = asyncio.run(main())
    assert r["skipped"] and "不存在" in r["message"]
    ev = list(ra.notices.items)[-1]
    assert ev["type"] == "run_error" and ev["route"] == "#/tasks"
    state.db.delete_task(t["id"])


def test_task_api_folder(ra, workspace, monkeypatch):
    with TestClient(app, base_url=PC) as c:
        r = c.post("/api/tasks", headers=H, json={"name": "x", "prompt": "y", "cron": "0 9 * * *", "watch_dir": "素材"})
        t = r.json()
        assert t["watch_dir"] == str(workspace / "素材") and (workspace / "素材").is_dir() and "watch_seen" not in t
        r = c.post(f"/api/tasks/{t['id']}/run", headers=H).json()
        assert r["skipped"] and not r["session_id"]
        c.delete(f"/api/tasks/{t['id']}", headers=H)


# ---------------------------------------------------------------------------
# approval on the phone, and how the publish went

@pytest.fixture()
def xhs_account():
    acc = state.db.create_account("xiaohongshu", "美食号")
    yield acc
    state.db.delete_account(acc["id"])


def test_unattended_publish_asks_on_the_phone(ra, xhs_account, tmp_path):
    video = put_file(tmp_path, "成片.mp4")
    session = state.db.create_session(title="⏰ 素材剪辑 · 10-17 18:00", kind="task")

    async def emit(_ev):
        return None

    ctx = ToolContext(session_id=session["id"], workspace=tmp_path, emit=emit, headless=True, publish_mode="queue")
    out = asyncio.run(social_publish(ctx, accounts=["小红书"], video=str(video), title="周末去哪儿"))
    assert "手机" in out["message"]
    pid = out["results"][0]["post_id"]
    ev = list(ra.notices.items)[-1]
    assert ev["type"] == "posts_pending" and ev["route"] == f"#/review/{pid}" and ev["pending"] == 1
    assert "周末去哪儿" in ev["text"] and "美食号" in ev["text"]

    _run_notice(session["id"], {"type": "run_done", "text": "做好了", "seconds": 300})    # the run ends
    ev = list(ra.notices.items)[-1]
    assert ev["type"] == "run_done" and ev["route"] == f"#/review/{pid}" and ev["pending"] == 1

    got = TestClient(app, base_url=PC).get(f"/api/social/posts?ids={pid},nope").json()     # the page it opens
    assert [p["id"] for p in got] == [pid] and got[0]["account_name"] == "美食号"


def test_interactive_queue_does_not_buzz(ra, xhs_account, tmp_path):
    video = put_file(tmp_path, "成片.mp4")
    session = state.db.create_session(title="聊天")

    async def emit(_ev):
        return None

    ctx = ToolContext(session_id=session["id"], workspace=tmp_path, emit=emit, publish_mode="queue")
    asyncio.run(social_publish(ctx, accounts=["小红书"], video=str(video), title="先别发"))
    assert not [e for e in ra.notices.items if e["type"] == "posts_pending"]


@pytest.mark.parametrize("result", ["ok", "fail", "verify"])
def test_publish_result_reaches_the_phone(ra, xhs_account, tmp_path, monkeypatch, result):
    video = put_file(tmp_path, "成片.mp4")
    session = state.db.create_session(title="⏰ 素材剪辑", kind="task")
    p1 = state.db.create_post(xhs_account["id"], "xiaohongshu", {"video": str(video), "title": "第一条"}, session_id=session["id"])
    p2 = state.db.create_post(xhs_account["id"], "xiaohongshu", {"video": str(video), "title": "第二条"}, session_id=session["id"])
    actives: list[int] = []

    async def fake_xhs(acc, content, progress, **kw):
        actives.append(len(social.PROGRESS))
        if result == "verify":
            await progress(0.05, "小红书要求安全验证，请在弹出的浏览器窗口里完成…")
        if result == "fail":
            raise RuntimeError("小红书没有发布，页面提示：发布太频繁")
        return {"url": "https://creator.xiaohongshu.com/new/note-manager"}

    monkeypatch.setattr(social, "_run_xhs", fake_xhs)
    asyncio.run(social.publish_post(p1["id"], announce=True))
    assert actives == [1] and not social.PROGRESS      # counted as work in progress only while it runs
    evs = [e for e in ra.notices.items if e.get("post_id") == p1["id"]]
    kinds = [e["type"] for e in evs]
    if result == "ok":
        assert kinds == ["post_published"] and "美食号" in evs[0]["text"] and evs[0]["url"]
    elif result == "fail":
        assert kinds == ["post_failed"] and "太频繁" in evs[0]["text"]
    else:
        assert kinds == ["post_attention", "post_published"] and "安全验证" in evs[0]["text"]
    last = evs[-1]
    assert last["pending"] == 1 and last["route"] == f"#/review/{p1['id']},{p2['id']}" and "还有 1 条" in last["text"]


def test_events_stay_active_while_publishing(ra, monkeypatch):
    dev, token = ra.pair(ra.pairing_code()["code"], "手机", "android", "1.2.3.4")
    monkeypatch.setitem(social.PROGRESS, "p1", {"progress": 0.5, "text": "上传中"})
    phone = TestClient(_Tag(app), base_url="http://192.168.1.20:8766")
    r = phone.get("/api/remote/events?after=0", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["active"] >= 1


def test_phone_learns_when_the_next_task_starts():
    t = state.db.create_task(name="每天", prompt="x", cron="0 9 * * *")

    async def main():
        sch = sched_mod.TaskScheduler()
        sch.sync(t["id"])
        sch.sched.start()
        try:
            return sch.seconds_to_next()
        finally:
            sch.shutdown()

    secs = asyncio.run(main())
    assert secs is not None and 0 <= secs <= 24 * 3600
    state.db.delete_task(t["id"])
