"""Sleep interval data structure used by huckleberry_sink and runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class SleepInterval:
    session_id: str
    start: datetime
    end: datetime
    total_seconds: float

    def __str__(self) -> str:
        return (
            f"SleepInterval(session={self.session_id!r}, "
            f"start={self.start.isoformat()}, end={self.end.isoformat()}, "
            f"duration={self.total_seconds/60:.1f}min)"
        )
