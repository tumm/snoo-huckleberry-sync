"""Sleep interval data structure used by huckleberry_sink."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class SleepInterval:
    session_id: str
    start: datetime
    end: datetime
    asleep_seconds: float
    total_seconds: float

    @property
    def asleep_fraction(self) -> float:
        return self.asleep_seconds / self.total_seconds if self.total_seconds > 0 else 0.0

    def __str__(self) -> str:
        return (
            f"SleepInterval(session={self.session_id!r}, "
            f"start={self.start.isoformat()}, end={self.end.isoformat()}, "
            f"duration={self.total_seconds/60:.1f}min)"
        )
