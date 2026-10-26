"""Start AutoStudio in the background when the user signs in to Windows, and put shortcuts on the desktop.

Autostart is one value under HKCU\\...\\Run (no admin rights) that starts launcher.py with pythonw.exe, so
no window appears; the scheduled tasks then run whether or not anybody opened AutoStudio that day.
"""
from __future__ import annotations

import base64
import logging
import subprocess
import sys
from pathlib import Path

from .config import DATA_DIR
from .launcher import ROOT, venv_python

log = logging.getLogger("autostudio.autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "AutoStudio"
ICON_PNG = Path(__file__).parent / "web" / "icons" / "icon-512.png"


def supported() -> bool:
    return sys.platform == "win32" and venv_python(windowless=True).exists()


def command() -> str:
    return f'"{venv_python(windowless=True)}" "{ROOT / "autostudio" / "launcher.py"}"'


def _winreg():
    import winreg

    return winreg


def enabled() -> bool:
    if sys.platform != "win32":
        return False
    reg = _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as k:
            value, _ = reg.QueryValueEx(k, NAME)
    except OSError:
        return False
    return str(value).strip().lower() == command().lower()


def set_enabled(on: bool) -> bool:
    if not supported():
        return False
    reg = _winreg()
    with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as k:
        if on:
            reg.SetValueEx(k, NAME, 0, reg.REG_SZ, command())
        else:
            try:
                reg.DeleteValue(k, NAME)
            except FileNotFoundError:
                pass
    return enabled() == on


def desktop() -> Path:
    """The real desktop folder (it may have been moved, e.g. into OneDrive)."""
    if sys.platform == "win32":
        try:
            import ctypes
            import uuid
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                            ("Data4", wintypes.BYTE * 8)]

            u = uuid.UUID("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}")      # FOLDERID_Desktop
            g = GUID(u.fields[0], u.fields[1], u.fields[2], (wintypes.BYTE * 8).from_buffer_copy(u.bytes[8:]))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
                path = Path(out.value or "")
                ctypes.windll.ole32.CoTaskMemFree(out)
                if path.is_dir():
                    return path
        except Exception:  # noqa: BLE001
            log.debug("SHGetKnownFolderPath failed", exc_info=True)
    return Path.home() / "Desktop"


def app_icon() -> Path | None:
    out = DATA_DIR / "AutoStudio.ico"
    if out.exists():
        return out
    try:
        from PIL import Image

        out.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(ICON_PNG) as im:
            im.save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return out
    except Exception:  # noqa: BLE001
        return None


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def shortcut(lnk: Path, target: Path, workdir: Path | None = None, icon: Path | None = None, description: str = "") -> bool:
    """Create a Windows .lnk shortcut (through WScript.Shell, available on every Windows)."""
    if sys.platform != "win32":
        return False
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(str(lnk))});"
              f"$s.TargetPath = {_ps_quote(str(target))};"
              + (f"$s.WorkingDirectory = {_ps_quote(str(workdir))};" if workdir else "")
              + (f"$s.IconLocation = {_ps_quote(str(icon))};" if icon else "")
              + (f"$s.Description = {_ps_quote(description)};" if description else "")
              + "$s.Save()")
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], capture_output=True,
                       timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    return r.returncode == 0 and lnk.exists()


def desktop_shortcuts(material: Path | None = None) -> list[str]:
    """「AutoStudio」 (opens the app) and, unless it already sits on the desktop, the material folder."""
    made: list[str] = []
    desk = desktop()
    bat = ROOT / "AutoStudio.bat"
    if bat.exists() and shortcut(desk / "AutoStudio.lnk", bat, ROOT, app_icon(), "打开 AutoStudio"):
        made.append(str(desk / "AutoStudio.lnk"))
    if material and material.is_dir() and material.parent != desk:
        link = desk / f"{material.name}.lnk"
        if shortcut(link, material, description="往这里放素材，AutoStudio 每天会自动处理"):
            made.append(str(link))
    return made
