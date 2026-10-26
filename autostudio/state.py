"""Process-wide singletons. Kept in one place so tools can reach the scheduler, DB, etc."""
from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from .config import DB_FILE, Settings
from .db import DB

if TYPE_CHECKING:
    from .agent.runs import RunManager
    from .scheduler import TaskScheduler

settings = Settings()
db = DB(DB_FILE)
runs: "RunManager | None" = None
scheduler: "TaskScheduler | None" = None

server: Any = None            # the uvicorn.Server running this app (None in tests)
port: int = 8765
exit_code = 0                 # what the process exits with; launcher.RESTART asks launcher.py to start it again
last_ui_request = time.time() # someone using the app on this PC (updates wait until nobody is)
supervised = os.environ.get("AUTOSTUDIO_SUPERVISED") == "1"


def request_exit(code: int = 0) -> bool:
    """Shut the app down (code 0: the user quit; 75: start again, see launcher.py)."""
    global exit_code
    if server is None:
        return False
    exit_code = code
    server.should_exit = True
    return True
