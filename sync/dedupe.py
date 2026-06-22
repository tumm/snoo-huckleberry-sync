"""SQLite-backed idempotency store.

written_sessions -sessions already synced to Huckleberry (permanent).
active_sessions  -sessions currently in progress on the SNOO (transient).
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS written_sessions (
    session_id   TEXT PRIMARY KEY,
    start_utc    TEXT NOT NULL,
    end_utc      TEXT NOT NULL,
    written_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active_sessions (
    session_id      TEXT PRIMARY KEY,
    start_ms        INTEGER NOT NULL,
    last_event_ms   INTEGER NOT NULL,
    first_seen      TEXT NOT NULL
);
"""


class DedupeStore:
    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_DDL)
        self._conn.commit()
        log.debug("Dedupe store opened at %s", db_path)

    # ---- written sessions ----

    def seen(self, session_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM written_sessions WHERE session_id = ?", (session_id,)
        )
        return cur.fetchone() is not None

    def mark(self, session_id: str, start: datetime, end: datetime) -> None:
        now = datetime.utcnow().isoformat()
        self._conn.execute(
            "INSERT OR IGNORE INTO written_sessions (session_id, start_utc, end_utc, written_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, start.isoformat(), end.isoformat(), now),
        )
        self._conn.commit()
        log.debug("Marked session %s as written", session_id)

    # ---- active session tracking ----

    def get_active_sessions(self) -> list[tuple[str, int, int]]:
        """Return list of (session_id, start_ms, last_event_ms) for sessions seen as active."""
        cur = self._conn.execute("SELECT session_id, start_ms, last_event_ms FROM active_sessions")
        return cur.fetchall()

    def record_active_session(self, session_id: str, start_ms: int, last_event_ms: int) -> None:
        now = datetime.utcnow().isoformat()
        self._conn.execute(
            "INSERT OR IGNORE INTO active_sessions (session_id, start_ms, last_event_ms, first_seen) "
            "VALUES (?, ?, ?, ?)",
            (session_id, start_ms, last_event_ms, now),
        )
        self._conn.commit()
        log.debug("Recorded active session %s (start_ms=%d)", session_id, start_ms)

    def update_active_session_event(self, session_id: str, last_event_ms: int) -> None:
        self._conn.execute(
            "UPDATE active_sessions SET last_event_ms = ? WHERE session_id = ?",
            (last_event_ms, session_id),
        )
        self._conn.commit()
        log.debug("Updated last_event_ms for session %s", session_id)

    def close_active_session(self, session_id: str) -> None:
        self._conn.execute(
            "DELETE FROM active_sessions WHERE session_id = ?", (session_id,)
        )
        self._conn.commit()
        log.debug("Closed active session %s", session_id)

    def close(self) -> None:
        self._conn.close()
