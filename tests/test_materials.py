"""Material folder: every file is made into something once — across tasks, copies, renames, crashes and restarts."""
import asyncio
import datetime as dt
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("AUTOSTUDIO_HOME", tempfile.mkdtemp(prefix="autostudio_test_"))

import pytest  # noqa: E402

from autostudio import remote as remote_mod  # noqa: E402
from autostudio import scheduler as sched_mod  # noqa: E402
from autostudio import state  # noqa: E402
from autostudio.agent.runs import RunManager  # noqa: E402
from autostudio.remote import RemoteAccess  # noqa: E402
from autostudio.server import _run_notice  # noqa: E402

OLD = time.time() - 3600
TODAY = dt.date.today().isoformat()


@pytest.fixture(autouse=True)
def no_leftover_accounts():
    before = {a["id"] for a in state.db.list_accounts()}
    yield
    for a in state.db.list_accounts():
        if a["id"] not in before:
            state.db.delete_account(a["id"])


@pytest.fixture()
def ra(tmp_path, monkeypatch):
    r = RemoteAccess(tmp_path / "remote.json")
    monkeypatch.setattr(remote_mod, "remote", r)
    monkeypatch.setattr("autostudio.server.remote", r)
    return r


def put_file(folder: Path, name: str, data: bytes | None = None, mtime: float = OLD) -> Path:
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(4096) if data is None else data)
    os.utime(p, (mtime, mtime))
    return p


def make_task(folder: Path, **kw):
    return state.db.create_task(name="素材剪辑", prompt="把新素材剪成短视频发小红书", cron="0 18 * * *",
                                watch_dir=str(folder), **kw)


class Agent:
    """Stands in for the model: records what each run was given, and fails / posts / crashes on request."""

    def __init__(self, monkeypatch):
        self.prompts: list[str] = []
        self.fail = False
        self.post_then_crash = False
        monkeypatch.setattr("autostudio.agent.loop.run_agent", self.run)

    async def run(self, run, sid, prompt, **kw):
        self.prompts.append(prompt)
        if self.post_then_crash:
            acc = state.db.list_accounts()[0] if state.db.list_accounts() else state.db.create_account("xiaohongshu", "测试号")
            state.db.create_post(acc["id"], "xiaohongshu", {"title": "做好了"}, session_id=sid)
            raise RuntimeError("连接断了")
        if self.fail:
            raise RuntimeError("模型挂了")
        return "做好了"


async def run_once(sch, task_id):
    r = await sch.run_task(task_id)
    if r and r.get("session_id"):
        await state.runs.wait(r["session_id"])
    return r


def scheduler():
    state.runs = RunManager(notify=_run_notice)
    return sched_mod.TaskScheduler()


# ---------------------------------------------------------------------------

def test_scan_folder(tmp_path):
    put_file(tmp_path, "b.mp4", mtime=OLD + 10)
    put_file(tmp_path, "a.jpg")
    put_file(tmp_path, "sub/c.mov", mtime=OLD + 20)
    put_file(tmp_path, "copying.mp4", mtime=time.time())             # still being copied in
    put_file(tmp_path, f"{sched_mod.USED_DIR}/2026-10-01/old.mp4")   # made into something before
    for junk in ("~$x.docx", ".hidden.mp4", "dl.crdownload", "Thumbs.db", ".git/obj"):
        put_file(tmp_path, junk)
    assert [f["rel"] for f in sched_mod.scan_folder(tmp_path)] == ["a.jpg", "b.mp4", "sub/c.mov"]


def test_fingerprint_is_about_content(tmp_path):
    big = os.urandom(5 << 20)
    a = put_file(tmp_path, "a.mp4", big)
    b = put_file(tmp_path, "renamed copy.mp4", big, mtime=OLD + 999)
    c = put_file(tmp_path, "c.mp4", big[:-1] + b"!")
    fp = sched_mod.fingerprint
    assert fp(a, a.stat().st_size) == fp(b, b.stat().st_size) != fp(c, c.stat().st_size)


def test_each_file_is_used_once(ra, tmp_path, monkeypatch):
    folder = tmp_path / "素材"
    folder.mkdir()
    agent = Agent(monkeypatch)
    t = make_task(folder)

    async def main():
        sch = scheduler()
        r = await run_once(sch, t["id"])        # empty folder: nothing runs
        assert r["skipped"] and "没有新文件" in r["message"] and not agent.prompts
        put_file(folder, "厨房.mp4")
        put_file(folder, "卫生间/before.jpg", mtime=OLD + 5)
        agent.fail = True
        r = await run_once(sch, t["id"])        # the run fails: the files are not used up...
        assert r["session_id"] and "厨房.mp4" in agent.prompts[-1] and "before.jpg" in agent.prompts[-1]
        assert (folder / "厨房.mp4").exists()
        agent.fail = False
        r = await run_once(sch, t["id"])        # ...so the next run gets them again, and says so
        assert len(agent.prompts) == 2 and "第 2 次尝试" in agent.prompts[-1]
        # used: moved into 已用过/<today>/, keeping the sub-folder; the emptied sub-folder is gone
        used = folder / sched_mod.USED_DIR / TODAY
        assert (used / "厨房.mp4").exists() and (used / "卫生间" / "before.jpg").exists()
        assert not (folder / "厨房.mp4").exists() and not (folder / "卫生间").exists()
        r = await run_once(sch, t["id"])        # nothing new any more
        assert r["skipped"] and len(agent.prompts) == 2
        put_file(folder, "第二天.mp4")
        await run_once(sch, t["id"])
        assert "第二天.mp4" in agent.prompts[-1] and "厨房.mp4" not in agent.prompts[-1].split("以前用过的素材")[0]

    asyncio.run(main())
    runs = state.db.list_task_runs(t["id"])
    assert [r["status"] for r in reversed(runs)] == ["skipped", "error", "ok", "skipped", "ok"]
    assert not [e for e in ra.notices.items if e.get("route") == "#/tasks"]     # skipping is not news


def test_same_video_copied_in_again_is_not_reused(ra, tmp_path, monkeypatch):
    folder = tmp_path / "素材"
    data = os.urandom(8192)
    put_file(folder, "IMG_0001.mp4", data)
    agent = Agent(monkeypatch)
    t = make_task(folder)

    async def main():
        sch = scheduler()
        await run_once(sch, t["id"])
        assert len(agent.prompts) == 1
        put_file(folder, "微信视频_20261025.mp4", data, mtime=OLD + 100)    # the same clip, saved again
        r = await run_once(sch, t["id"])
        assert r["skipped"] and len(agent.prompts) == 1
        assert not (folder / "微信视频_20261025.mp4").exists()                # tidied away into 已用过
        assert (folder / sched_mod.USED_DIR / TODAY / "微信视频_20261025.mp4").exists()

    asyncio.run(main())


def test_two_copies_in_one_batch_go_in_once(ra, tmp_path, monkeypatch):
    folder = tmp_path / "素材"
    data = os.urandom(8192)
    put_file(folder, "a.mp4", data)
    put_file(folder, "a - 副本.mp4", data, mtime=OLD + 1)
    agent = Agent(monkeypatch)
    t = make_task(folder)

    async def main():
        await run_once(scheduler(), t["id"])

    asyncio.run(main())
    note = agent.prompts[-1]
    assert "a.mp4" in note and "副本" not in note
    assert not list(folder.glob("*.mp4"))       # both copies moved along


def test_two_tasks_on_one_folder_share_the_ledger(ra, tmp_path, monkeypatch):
    folder = tmp_path / "素材"
    put_file(folder, "x.mp4")
    agent = Agent(monkeypatch)
    t1, t2 = make_task(folder), make_task(folder)

    async def main():
        sch = scheduler()
        await run_once(sch, t1["id"])
        put_file(folder, "x-again.mp4", (folder / sched_mod.USED_DIR / TODAY / "x.mp4").read_bytes())
        r = await run_once(sch, t2["id"])
        assert r["skipped"]

    asyncio.run(main())
    assert len(agent.prompts) == 1


def test_a_post_counts_even_if_the_run_crashes_after_it(ra, tmp_path, monkeypatch):
    """The original bug: the post was made, then the run failed, and the next day the same video was made again."""
    folder = tmp_path / "素材"
    put_file(folder, "工地.mp4")
    agent = Agent(monkeypatch)
    agent.post_then_crash = True
    t = make_task(folder)

    async def main():
        sch = scheduler()
        await run_once(sch, t["id"])
        agent.post_then_crash = False
        r = await run_once(sch, t["id"])
        assert r["skipped"]

    asyncio.run(main())
    assert len(agent.prompts) == 1


def test_claims_left_by_a_closed_app(ra, tmp_path, monkeypatch):
    folder = tmp_path / "素材"
    posted, lost = put_file(folder, "posted.mp4"), put_file(folder, "lost.mp4", mtime=OLD + 1)
    t = make_task(folder)
    acc = state.db.create_account("xiaohongshu", "测试号2")
    # the app was closed mid-run: one run had already queued its post, the other had not got that far
    for f, sid, post in ((posted, "deadsession1", True), (lost, "deadsession2", False)):
        fp = sched_mod.fingerprint(f, f.stat().st_size)
        state.db.put_material(fp, name=f.name, path=str(f), folder=str(folder), status="claimed", session_id=sid)
        if post:
            state.db.create_post(acc["id"], "xiaohongshu", {"title": "t"}, session_id=sid)
    agent = Agent(monkeypatch)

    async def main():
        await run_once(scheduler(), t["id"])

    asyncio.run(main())
    note = agent.prompts[-1]
    assert "lost.mp4" in note and "第 2 次尝试" in note and "posted.mp4" not in note.split("以前用过的素材")[0]
    assert (folder / sched_mod.USED_DIR / TODAY / "posted.mp4").exists()


def test_gives_up_after_three_tries_and_can_retry(ra, tmp_path, monkeypatch):
    folder = tmp_path / "素材"
    put_file(folder, "坏文件.mp4")
    agent = Agent(monkeypatch)
    agent.fail = True
    t = make_task(folder)

    async def main():
        sch = scheduler()
        for _ in range(sched_mod.MAX_ATTEMPTS):
            await run_once(sch, t["id"])
        r = await run_once(sch, t["id"])
        assert r["skipped"] and "没做成" in r["message"]
        assert len(agent.prompts) == sched_mod.MAX_ATTEMPTS
        gave_up = [e for e in ra.notices.items if e.get("route") == "#/tasks"]
        assert len(gave_up) == 1 and "坏文件.mp4" in gave_up[0]["text"]
        summary = sched_mod.material_summary(folder)
        assert summary["failed"] == 1 and summary["failed_files"][0]["name"] == "坏文件.mp4"
        assert (folder / "坏文件.mp4").exists()      # left where it was: it was never used
        assert sched_mod.retry_failed(folder) == 1
        agent.fail = False
        await run_once(sch, t["id"])
        assert len(agent.prompts) == sched_mod.MAX_ATTEMPTS + 1

    asyncio.run(main())


def test_files_used_by_the_old_per_task_ledger_stay_used(ra, tmp_path, monkeypatch):
    folder = tmp_path / "素材"
    old = put_file(folder, "旧.mp4")
    st = old.stat()
    t = make_task(folder)
    state.db.set_task_seen(t["id"], {"dir": str(folder), "files": {"旧.mp4": f"{st.st_size}:{int(st.st_mtime)}"}})
    put_file(folder, "新.mp4", mtime=OLD + 1)
    agent = Agent(monkeypatch)

    async def main():
        await run_once(scheduler(), t["id"])

    asyncio.run(main())
    note = agent.prompts[-1].split("以前用过的素材")[0]
    assert "新.mp4" in note and "旧.mp4" not in note


def test_missed_run_is_made_up_when_the_app_starts(ra, tmp_path, monkeypatch):
    folder = tmp_path / "素材"
    put_file(folder, "a.mp4")
    agent = Agent(monkeypatch)
    now = dt.datetime.now().astimezone()
    due = now - dt.timedelta(hours=2)
    t = make_task(folder)
    state.db.update_task(t["id"], cron=f"{due.minute} {due.hour} * * *")
    state.db._exec("UPDATE tasks SET created_at=? WHERE id=?", (time.time() - 86400 * 3, t["id"]))

    last = sched_mod.TaskScheduler.last_due(f"{due.minute} {due.hour} * * *", now)
    assert last and abs(last.timestamp() - due.replace(second=0, microsecond=0).timestamp()) < 1

    async def main():
        sch = scheduler()
        started = await sch.catch_up()
        if started:
            await state.runs.wait(sch.active.get(t["id"]) or "")
        await asyncio.sleep(0.05)
        assert t["id"] in started
        assert await sch.catch_up() == []      # it ran: nothing to make up any more

    asyncio.run(main())
    assert len(agent.prompts) == 1
