"""Updating itself, starting with Windows, and the first-run setup."""
import asyncio
import hashlib
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("AUTOSTUDIO_HOME", tempfile.mkdtemp(prefix="autostudio_test_"))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from autostudio import autostart, launcher, state, updater as updater_mod  # noqa: E402
from autostudio import server as server_mod  # noqa: E402
from autostudio.config import MEMORY_FILE, VERSION  # noqa: E402
from autostudio.remote import _Tag  # noqa: E402

H = {"X-AutoStudio": "1"}
PC = "http://127.0.0.1:8765"


def release_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


APP = {"AutoStudio/autostudio/__main__.py": "print('new')\n", "AutoStudio/autostudio/config.py": 'VERSION = "9.9.9"\n',
       "AutoStudio/README.md": "new readme\n"}


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    """launcher.py working on a throwaway install folder and data folder."""
    root, data = tmp_path / "AutoStudio", tmp_path / "data"
    (root / "autostudio").mkdir(parents=True)
    (root / ".venv").mkdir()
    (root / ".venv" / "keep.txt").write_text("venv")
    (root / "autostudio" / "__main__.py").write_text("print('old')\n")
    (root / "autostudio" / "config.py").write_text('VERSION = "0.1.0"\n')
    (root / "autostudio" / "removed_in_new.py").write_text("old")
    (root / "notes-of-the-user.txt").write_text("mine")
    (root / launcher.MANIFEST.name).write_text("autostudio/__main__.py\nautostudio/config.py\nautostudio/removed_in_new.py\n")
    up = data / "update"
    for name, value in (("ROOT", root), ("DATA_DIR", data), ("UPDATE_DIR", up), ("READY_FILE", up / "ready.json"),
                        ("APPLIED_FILE", up / "applied.json"), ("MANIFEST", root / launcher.MANIFEST.name), ("LOG_DIR", data / "logs")):
        monkeypatch.setattr(launcher, name, value)
    monkeypatch.setattr(updater_mod, "READY_FILE", up / "ready.json")
    monkeypatch.setattr(updater_mod, "UPDATE_DIR", up)
    monkeypatch.setattr(updater_mod, "APPLIED_FILE", up / "applied.json")
    monkeypatch.setattr(updater_mod, "is_checkout", lambda: (root / ".git").exists())
    return root, data


def test_versions():
    assert updater_mod.newer("0.2.0", "0.1.9") and updater_mod.newer("0.10.0", "0.9.9") and updater_mod.newer("1.0", "0.99.99")
    assert not updater_mod.newer("0.2.0", "0.2.0") and not updater_mod.newer("0.1.0", "0.2.0")


def test_unpack_refuses_strange_paths(tmp_path):
    z = tmp_path / "bad.zip"
    z.write_bytes(release_zip({**APP, "AutoStudio/../../evil.txt": "x"}))
    with pytest.raises(ValueError):
        updater_mod.unpack(z, tmp_path / "out")
    z.write_bytes(release_zip({"AutoStudio/README.md": "no app in here"}))
    with pytest.raises(ValueError):
        updater_mod.unpack(z, tmp_path / "out")


def fake_site(zip_bytes: bytes, version: str = "9.9.9", digest: str | None = None):
    info = {"version": version, "notes": "更好用了", "zip": {"url": "/AutoStudio.zip", "sha256": digest or hashlib.sha256(zip_bytes).hexdigest(),
                                                           "size": len(zip_bytes)}}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/update.json":
            return httpx.Response(200, json=info)
        if req.url.path == "/AutoStudio.zip":
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_download_verify_and_install(paths, monkeypatch):
    root, data = paths
    monkeypatch.setattr(state, "supervised", True)
    up = updater_mod.Updater()
    up.transport = fake_site(release_zip(APP))
    monkeypatch.setattr(up, "url", lambda: "https://example.test/update.json")
    view = asyncio.run(up.check(download=True))
    assert view["state"] == "ready" and view["latest"] == "9.9.9", view
    ready = json.loads(launcher.READY_FILE.read_text("utf-8"))
    assert ready["version"] == "9.9.9" and Path(ready["dir"], "autostudio", "__main__.py").is_file()

    # the launcher installs it on the way back up
    assert launcher.apply_update() == "9.9.9"
    assert (root / "autostudio" / "__main__.py").read_text() == "print('new')\n"
    assert not (root / "autostudio" / "removed_in_new.py").exists()     # dropped by the new version
    assert (root / "notes-of-the-user.txt").read_text() == "mine"         # never ours: left alone
    assert (root / ".venv" / "keep.txt").exists()
    assert "autostudio/__main__.py" in (root / launcher.MANIFEST.name).read_text()
    assert not launcher.READY_FILE.exists() and json.loads(launcher.APPLIED_FILE.read_text("utf-8"))["version"] == "9.9.9"
    assert not list(root.rglob("*.autostudio-new"))


def test_tampered_download_is_dropped(paths, monkeypatch):
    monkeypatch.setattr(state, "supervised", True)
    up = updater_mod.Updater()
    up.transport = fake_site(release_zip(APP), digest="0" * 64)
    monkeypatch.setattr(up, "url", lambda: "https://example.test/update.json")
    view = asyncio.run(up.check(download=True))
    assert view["state"] == "error" and "校验" in view["error"] and not launcher.READY_FILE.exists()


def test_never_installs_into_a_git_checkout(paths, monkeypatch):
    root, _ = paths
    (root / ".git").mkdir()
    monkeypatch.setattr(state, "supervised", True)
    up = updater_mod.Updater()
    up.transport = fake_site(release_zip(APP))
    monkeypatch.setattr(up, "url", lambda: "https://example.test/update.json")
    view = asyncio.run(up.check())
    assert view["state"] == "available" and not view["can_install"] and not launcher.READY_FILE.exists()
    # even with an update lying around, the launcher leaves a checkout alone
    launcher.UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    launcher.READY_FILE.write_text(json.dumps({"version": "9.9.9", "dir": str(root)}))
    assert launcher.apply_update() is None and (root / "autostudio" / "__main__.py").read_text() == "print('old')\n"


def test_up_to_date(paths, monkeypatch):
    up = updater_mod.Updater()
    up.transport = fake_site(b"", version=VERSION)
    monkeypatch.setattr(up, "url", lambda: "https://example.test/update.json")
    assert asyncio.run(up.check())["state"] == "latest"


def test_waits_until_idle(monkeypatch):
    import time

    from autostudio.agent.runs import RunManager

    monkeypatch.setattr(state, "runs", RunManager())
    monkeypatch.setattr(state, "scheduler", None)
    monkeypatch.setattr(state, "last_ui_request", time.time())
    assert not updater_mod.Updater.idle()                 # someone is using the app
    monkeypatch.setattr(state, "last_ui_request", time.time() - 3600)
    assert updater_mod.Updater.idle()


def test_setup_fingerprint_matches_the_installer():
    fp = launcher.setup_fingerprint()
    assert fp and fp.startswith("v") and len(fp.split(" ")[1]) == 64 and fp.split(" ")[1].isupper()


# ---------------------------------------------------------------------------
# start with Windows

class FakeReg:
    HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ = 1, 2, 1

    def __init__(self):
        self.values: dict[str, str] = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, *a):
        return self._Key()

    def CreateKeyEx(self, *a):
        return self._Key()

    def QueryValueEx(self, _k, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], 1

    def SetValueEx(self, _k, name, _r, _t, value):
        self.values[name] = value

    def DeleteValue(self, _k, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_autostart_run_key(monkeypatch):
    reg = FakeReg()
    monkeypatch.setattr(autostart, "_winreg", lambda: reg)
    monkeypatch.setattr(autostart, "supported", lambda: True)
    assert not autostart.enabled()
    assert autostart.set_enabled(True) and autostart.enabled()
    assert "launcher.py" in reg.values["AutoStudio"] and "pythonw.exe" in reg.values["AutoStudio"]
    assert autostart.set_enabled(False) and "AutoStudio" not in reg.values


# ---------------------------------------------------------------------------
# first run

def test_first_run_setup_creates_the_daily_task(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "supported", lambda: False)
    monkeypatch.setattr(autostart, "desktop_shortcuts", lambda folder: [])
    monkeypatch.setattr(autostart, "desktop", lambda: tmp_path / "Desktop")
    opened = []

    async def fake_reveal(body):
        opened.append(body["path"])
        return {"ok": True}

    monkeypatch.setattr(server_mod, "reveal", fake_reveal)
    acc = state.db.create_account("xiaohongshu", "温哥华装修老李")
    old_memory = MEMORY_FILE.read_text("utf-8") if MEMORY_FILE.exists() else None
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text("## 字幕偏好\n黄色描边\n", "utf-8")
    try:
        with TestClient(server_mod.app, base_url=PC) as c:
            st = c.get("/api/setup").json()
            assert st["default_folder"] == str(tmp_path / "Desktop" / "素材")
            r = c.post("/api/setup/finish", headers=H, json={"about": "我在温哥华做室内装修，主要做厨房和卫生间翻新。",
                                                              "time": "20:30", "accounts": [acc["id"]]})
            assert r.status_code == 200, r.text
            d = r.json()
            t = d["task"]
            assert t["cron"] == "30 20 * * *" and t["watch_dir"] == str(tmp_path / "Desktop" / "素材") and t["enabled"]
            assert "温哥华装修老李" in t["prompt"] and not t["auto_publish"] and t["allow_dangerous"]
            assert (tmp_path / "Desktop" / "素材").is_dir() and opened == [t["watch_dir"]]
            assert d["setup"]["done"] and "厨房" in d["setup"]["about"]
            memory = MEMORY_FILE.read_text("utf-8")
            assert memory.startswith("## 关于我\n我在温哥华") and "黄色描边" in memory
            # running it again changes the same task, and the 关于我 section is replaced, not repeated
            r = c.post("/api/setup/finish", headers=H, json={"about": "我做装修。", "time": "7:05"})
            assert r.json()["task"]["id"] == t["id"] and r.json()["task"]["cron"] == "5 7 * * *"
            memory = MEMORY_FILE.read_text("utf-8")
            assert memory.count("## 关于我") == 1 and "我做装修。" in memory and "温哥华" not in memory
            assert c.post("/api/setup/finish", headers=H, json={"time": "25:99"}).status_code == 400
            c.delete(f"/api/tasks/{t['id']}", headers=H)
    finally:
        state.db.delete_account(acc["id"])
        if old_memory is None:
            MEMORY_FILE.unlink(missing_ok=True)
        else:
            MEMORY_FILE.write_text(old_memory, "utf-8")
        state.settings.update({"setup": {"done": False, "task_id": None}})


def test_phones_cannot_quit_or_reconfigure_the_pc(tmp_path, monkeypatch):
    from autostudio.remote import RemoteAccess

    r = RemoteAccess(tmp_path / "remote.json")
    monkeypatch.setattr("autostudio.server.remote", r)
    _dev, token = r.pair(r.pairing_code()["code"], "手机", "android", "1.2.3.4")
    phone = TestClient(_Tag(server_mod.app), base_url="http://192.168.1.20:8766")
    auth = {**H, "Authorization": f"Bearer {token}"}
    for path in ("/api/app/quit", "/api/app/update/install", "/api/setup/finish"):
        assert phone.post(path, headers=auth, json={}).status_code == 403, path
    assert phone.put("/api/app/autostart", headers=auth, json={"enabled": False}).status_code == 403
    info = phone.get("/api/remote/app-update", headers=auth)
    assert info.status_code in (200, 404)
    if info.status_code == 200:
        assert info.json()["url"] == "/download/AutoStudio.apk" and len(info.json()["sha256"]) == 64
