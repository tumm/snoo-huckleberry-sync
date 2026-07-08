"""Shared domain models used across sync modules."""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class SnooCompletedSession:
    session_id: str
    start: datetime  # aware datetime in UTC
    end: datetime    # aware datetime in UTC
    total_seconds: float
    notes: str
