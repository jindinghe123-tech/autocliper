"""Environment check used by the launchers: prints a checklist, exits 1 if a core dependency is missing.

Run with the project's Python:  python scripts/doctor.py
"""
from __future__ import annotations

import importlib
import sys

CORE = [
    ("fastapi", "Web 服务"),
    ("uvicorn", "Web 服务"),
    ("httpx", "网络请求"),
    ("multipart", "文件上传"),
    ("apscheduler", "定时任务"),
    ("numpy", "音频分析"),
    ("PIL", "逐帧画面处理"),
]
OPTIONAL = [
    ("faster_whisper", "语音识别字幕"),
    ("edge_tts", "AI 配音"),
    ("sherpa_onnx", "离线中文配音"),
    ("yt_dlp", "视频下载"),
    ("playwright", "发布到小红书"),
]


def _check(mod: str) -> str | None:
    try:
        importlib.import_module(mod)
        return None
    except Exception as e:  # noqa: BLE001 - DLL load errors etc. count as missing too
        return f"{type(e).__name__}: {e}".splitlines()[0][:120]


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    ok = True
    v = sys.version_info
    py_ok = v >= (3, 10)
    ok &= py_ok
    print(f"  {'[OK]' if py_ok else '[X] '} Python {v.major}.{v.minor}.{v.micro}")

    for mod, label in CORE:
        err = _check(mod)
        ok &= err is None
        print(f"  {'[OK]' if err is None else '[X] '} {label} ({mod})" + (f"  -> {err}" if err else ""))

    try:
        sys.path.insert(0, ".")
        from autostudio.media.ffmpeg import ffmpeg_bin

        print(f"  [OK] ffmpeg  {ffmpeg_bin()}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  [X]  ffmpeg  -> {e}")

    for mod, label in OPTIONAL:
        err = _check(mod)
        print(f"  {'[OK]' if err is None else '[--]'} {label} ({mod})" + (f"  -> 不可用：{err}" if err else ""))

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
