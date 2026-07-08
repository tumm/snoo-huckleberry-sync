"""SQLite-backed idempotency store.

written_sessions    - sessions already synced to Huckleberry (permanent).
active_sessions     - sessions currently in progress on the SNOO (transient; basic mode only).
live_session_events - per-state-transition event log for in-progress sessions (transient; live mode only).
"""

import logging
import sqlite3
import threading
from datetime import UTC, datetime
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
CREATE TABLE IF NOT EXISTS live_session_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    event_time_ms INTEGER NOT NULL,
    state         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_session_events_session_id ON live_session_events(session_id);
"""


class DedupeStore:
    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(_DDL)
        self._conn.commit()
        log.debug("Dedupe store opened at %s", db_path)

    # ---- written sessions ----

    def seen(self, session_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM written_sessions WHERE session_id = ?", (session_id,)
            )
            return cur.fetchone() is not None

    def mark(self, session_id: str, start: datetime, end: datetime) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO written_sessions (session_id, start_utc, end_utc, written_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, start.isoformat(), end.isoformat(), now),
            )
            self._conn.commit()
        log.debug("Marked session %s as written", session_id)

    # ---- active session tracking (non-premium device-polling mode) ----

    def get_active_sessions(self) -> list[tuple[str, int, int]]:
        """Return list of (session_id, start_ms, last_event_ms) for sessions seen as active."""
        with self._lock:
            cur = self._conn.execute("SELECT session_id, start_ms, last_event_ms FROM active_sessions")
            return cur.fetchall()

    def record_active_session(self, session_id: str, start_ms: int, last_event_ms: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO active_sessions (session_id, start_ms, last_event_ms, first_seen) "
                "VALUES (?, ?, ?, ?)",
                (session_id, start_ms, last_event_ms, now),
            )
            self._conn.commit()
        log.debug("Recorded active session %s (start_ms=%d)", session_id, start_ms)

    def update_active_session_event(self, session_id: str, last_event_ms: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE active_sessions SET last_event_ms = ? WHERE session_id = ?",
                (last_event_ms, session_id),
            )
            self._conn.commit()
        log.debug("Updated last_event_ms for session %s", session_id)

    def close_active_session(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM active_sessions WHERE session_id = ?", (session_id,)
            )
            self._conn.commit()
        log.debug("Closed active session %s", session_id)

    # ---- live session event tracking (live MQTT mode) ----

    def append_live_event(self, session_id: str, event_time_ms: int, state: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO live_session_events (session_id, event_time_ms, state) VALUES (?, ?, ?)",
                (session_id, event_time_ms, state),
            )
            self._conn.commit()

    def get_live_events(self, session_id: str) -> list[tuple[int, str]]:
        """Return (event_time_ms, state) rows for a session, oldest first."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT event_time_ms, state FROM live_session_events WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            )
            return cur.fetchall()

    def clear_live_events(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM live_session_events WHERE session_id = ?", (session_id,))
            self._conn.commit()

    def open_live_session_ids(self) -> list[str]:
        """Distinct session_ids currently being tracked, oldest-opened first."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT session_id FROM live_session_events GROUP BY session_id ORDER BY MIN(id)"
            )
            return [row[0] for row in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
