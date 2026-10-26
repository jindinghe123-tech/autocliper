"""Keeps AutoStudio running in the background: starts it, restarts it after a crash, and installs updates.

Started by AutoStudio.bat once setup is done, and when the user signs in to Windows (autostart.py). It runs
with pythonw.exe, so there is no console window, and it uses only the standard library: updating the app's
packages never has to replace anything this process has loaded.

    python autostudio/launcher.py            supervise (one launcher per data folder; a second one just exits)

The app asks for things through its exit code: 0 = the user quit, RESTART = start me again (an update was
downloaded and is ready, see updater.py). Before each start the launcher
  1. installs a downloaded update (copies the new files over this folder; never in a git checkout),
  2. re-installs packages when pyproject.toml changed (runs scripts/setup-windows.ps1 --setup-only),
and then starts `python -m autostudio --no-browser` and writes the port to <data>/server.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("AUTOSTUDIO_HOME", Path.home() / ".autostudio")).expanduser()
LOG_DIR = DATA_DIR / "logs"
SERVER_FILE = DATA_DIR / "server.json"
UPDATE_DIR = DATA_DIR / "update"
READY_FILE = UPDATE_DIR / "ready.json"       # written by updater.py when a verified update is unpacked
APPLIED_FILE = UPDATE_DIR / "applied.json"   # read by the app to say "updated to x.y.z"
MANIFEST = ROOT / ".autostudio-files.txt"    # the app's own files, so an update can remove the ones it dropped
RESTART = 75
PORTS = range(8765, 8780)
KEEP = {".venv", ".runtime", ".git", MANIFEST.name}
IS_WIN = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [launcher] {msg}\n"
    with open(LOG_DIR / "launcher.log", "a", encoding="utf-8") as f:
        f.write(line)
    if sys.stdout and not IS_WIN:
        sys.stdout.write(line)


def venv_python(windowless: bool = False) -> Path:
    if IS_WIN:
        return ROOT / ".venv" / "Scripts" / ("pythonw.exe" if windowless else "python.exe")
    return ROOT / ".venv" / "bin" / "python"


def is_checkout() -> bool:
    """A developer's git checkout updates through git, never by having files copied over it."""
    return (ROOT / ".git").exists()


# ---------------------------------------------------------------------------
# one launcher per data folder

_lock_handle = None


def single_instance() -> bool:
    global _lock_handle
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = open(DATA_DIR / "launcher.lock", "a+")
    try:
        if IS_WIN:
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _lock_handle = f
    return True


# ---------------------------------------------------------------------------
# updates

def apply_update() -> str | None:
    """Install a downloaded update if one is ready. Returns the new version, or None."""
    if not READY_FILE.exists():
        return None
    try:
        ready = json.loads(READY_FILE.read_text("utf-8"))
        src = Path(ready["dir"])
        version = str(ready["version"])
    except (OSError, ValueError, KeyError):
        READY_FILE.unlink(missing_ok=True)
        return None
    if is_checkout() or not (src / "autostudio" / "__main__.py").is_file():
        log(f"update {ready.get('version')} not applied ({'git checkout' if is_checkout() else 'incomplete download'})")
        READY_FILE.unlink(missing_ok=True)
        return None
    prev = installed_version()
    new_files = sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file())
    old_files = MANIFEST.read_text("utf-8").splitlines() if MANIFEST.exists() else []
    staged: list[tuple[Path, Path]] = []
    try:
        for rel in new_files:     # phase 1: put every new file next to its target
            if rel.split("/", 1)[0] in KEEP:
                continue
            dest = ROOT / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".autostudio-new")
            shutil.copy2(src / rel, tmp)
            staged.append((tmp, dest))
        for tmp, dest in staged:  # phase 2: swap them in
            os.replace(tmp, dest)
    except OSError as e:
        for tmp, _dest in staged:
            tmp.unlink(missing_ok=True)
        log(f"update {version} failed while copying ({e}); will try again next start")
        return None
    gone = set(old_files) - set(new_files)
    for rel in sorted(gone):
        p = (ROOT / rel).resolve()
        if ROOT in p.parents and rel.split("/", 1)[0] not in KEEP and p.is_file():
            p.unlink(missing_ok=True)
    MANIFEST.write_text("\n".join(new_files) + "\n", "utf-8")
    APPLIED_FILE.write_text(json.dumps({"version": version, "at": time.time(), "notes": ready.get("notes", "")},
                                       ensure_ascii=False), "utf-8")
    READY_FILE.unlink(missing_ok=True)
    shutil.rmtree(src.parent, ignore_errors=True)
    log(f"updated {prev or '?'} -> {version} ({len(staged)} files, {len(gone)} removed)")
    return version


def installed_version() -> str:
    try:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', (ROOT / "autostudio" / "config.py").read_text("utf-8"), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def setup_fingerprint() -> str | None:
    """The same "v<n> <sha256 of pyproject.toml>" stamp scripts/setup-windows.ps1 writes after installing."""
    try:
        ps = (ROOT / "scripts" / "setup-windows.ps1").read_text("utf-8-sig")
        n = re.search(r"^\$SetupVersion\s*=\s*(\d+)", ps, re.M).group(1)  # type: ignore[union-attr]
        digest = hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest().upper()
        return f"v{n} {digest}"
    except Exception:  # noqa: BLE001
        return None


def ensure_packages() -> None:
    """After an update changed the dependencies, install them before starting (Windows installs only)."""
    if not IS_WIN or is_checkout():
        return
    want = setup_fingerprint()
    stamp = ROOT / ".venv" / "autostudio-setup.txt"
    have = stamp.read_text("utf-8").strip() if stamp.exists() else ""
    if not want or want == have:
        return
    log("packages changed: running setup")
    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    with open(LOG_DIR / "setup.log", "a", encoding="utf-8") as out:
        try:
            r = subprocess.run([str(ps), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                str(ROOT / "scripts" / "setup-windows.ps1"), "--setup-only"],
                               cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, creationflags=NO_WINDOW, timeout=3600)
            log(f"setup finished with {r.returncode}")
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"setup failed: {e}")


# ---------------------------------------------------------------------------
# running the app

def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port() -> int:
    try:
        phone = int(json.loads((DATA_DIR / "remote.json").read_text("utf-8")).get("port") or 8766)
    except (OSError, ValueError):
        phone = 8766
    for p in PORTS:
        if p not in (phone, 8766) and port_free(p):
            return p
    return 8765


def run_once() -> int:
    port = pick_port()
    env = {**os.environ, "AUTOSTUDIO_SUPERVISED": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = open(LOG_DIR / "autostudio.log", "a", encoding="utf-8")
    try:
        proc = subprocess.Popen([str(venv_python()), "-m", "autostudio", "--no-browser", "--port", str(port)], cwd=ROOT,
                                env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except OSError as e:
        log(f"cannot start: {e}")
        out.close()
        return 1
    SERVER_FILE.write_text(json.dumps({"port": port, "pid": proc.pid, "launcher_pid": os.getpid(), "started": time.time(),
                                       "url": f"http://127.0.0.1:{port}"}), "utf-8")
    log(f"started on port {port} (pid {proc.pid})")
    rc = proc.wait()
    out.close()
    log(f"exited with {rc}")
    return rc


def trim_logs() -> None:
    for f in LOG_DIR.glob("*.log"):
        try:
            if f.stat().st_size > 20 * 1048576:
                data = f.read_bytes()[-5 * 1048576:]
                f.write_bytes(data)
        except OSError:
            pass


def relaunch_self() -> None:
    """Start the launcher's own new version (after an update) and step aside."""
    exe = venv_python(windowless=True)
    if not exe.exists():
        exe = Path(sys.executable)
    subprocess.Popen([str(exe), str(Path(__file__).resolve())], cwd=ROOT, creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def main() -> int:
    os.chdir(ROOT)
    if not single_instance():
        return 0        # already supervised
    trim_logs()
    crashes: list[float] = []
    first = True
    while True:
        if apply_update() and not first:
            global _lock_handle
            if _lock_handle:
                _lock_handle.close()
                _lock_handle = None
            relaunch_self()
            return 0
        first = False
        ensure_packages()
        rc = run_once()
        if rc == 0:
            SERVER_FILE.unlink(missing_ok=True)
            return 0
        if rc == RESTART:
            continue
        now = time.time()
        crashes = [t for t in crashes if now - t < 600] + [now]
        wait = 600 if len(crashes) >= 5 else min(300, 5 * 2 ** (len(crashes) - 1))
        log(f"crashed; starting again in {wait}s")
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
