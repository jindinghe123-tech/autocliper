"""Keep the PC awake while AutoStudio is working.

A scheduled run can take most of an hour and nobody is at the PC, so Windows would happily go to sleep
halfway through an export or an upload. While anything holds this (an agent run, a publish), Windows is
told the system is required; the display may still turn off. Call hold/release from the event-loop
thread: Windows ties the request to the thread that made it.
"""
from __future__ import annotations

import logging
import sys
import threading

log = logging.getLogger("autostudio.power")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_lock = threading.Lock()
_holders = 0


def _set(awake: bool) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if awake else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:  # noqa: BLE001 - staying awake is a nicety, never a reason to fail
        log.debug("SetThreadExecutionState failed", exc_info=True)


def hold() -> None:
    global _holders
    with _lock:
        _holders += 1
        if _holders == 1:
            _set(True)


def release() -> None:
    global _holders
    with _lock:
        _holders = max(0, _holders - 1)
        if _holders == 0:
            _set(False)


def busy() -> int:
    return _holders
