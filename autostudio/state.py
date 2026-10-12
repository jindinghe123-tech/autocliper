"""Process-wide singletons. Kept in one place so tools can reach the scheduler, DB, etc."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .config import DB_FILE, Settings
from .db import DB

if TYPE_CHECKING:
    from .agent.runs import RunManager
    from .scheduler import TaskScheduler

settings = Settings()
db = DB(DB_FILE)
runs: "RunManager | None" = None
scheduler: "TaskScheduler | None" = None
