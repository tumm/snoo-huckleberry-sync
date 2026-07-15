"""SQLite-backed idempotency store.

written_sessions    - sessions already synced to Huckleberry (permanent).
active_sessions     - sessions currently in progress on the SNOO (transient; basic mode only).
live_session_events - per-state-transition event log for in-progress sessions (transient; live mode only).
failed_writes       - completed sessions whose Huckleberry write failed, kept for retry (transient; live mode only).

Not thread-safe by design: python-snoo's live subscription delivers events via
`async for message in client.messages` inside a task scheduled with
asyncio.create_task - there is no separate MQTT callback thread. Everything in
this process runs on the single asyncio event-loop OS thread, and no method
here contains an `await`, so none can be interleaved mid-call by the event
loop either. A threading.Lock would protect against a race that cannot occur
in this codebase's architecture.
"""

import logging
import sqlite3
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
CREATE TABLE IF NOT EXISTS failed_writes (
    session_id    TEXT PRIMARY KEY,
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL,
    total_seconds REAL NOT NULL,
    notes         TEXT NOT NULL,
    failed_at     TEXT NOT NULL
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
        now = datetime.now(UTC).isoformat()
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
        cur = self._conn.execute("SELECT session_id, start_ms, last_event_ms FROM active_sessions")
        return cur.fetchall()

    def record_active_session(self, session_id: str, start_ms: int, last_event_ms: int) -> None:
        now = datetime.now(UTC).isoformat()
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

    # ---- failed-write outbox (live mode) ----

    def save_failed_write(
        self, session_id: str, start: datetime, end: datetime, total_seconds: float, notes: str
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            "INSERT OR IGNORE INTO failed_writes "
            "(session_id, start_utc, end_utc, total_seconds, notes, failed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, start.isoformat(), end.isoformat(), total_seconds, notes, now),
        )
        self._conn.commit()

    def get_failed_writes(self) -> list[tuple[str, str, str, float, str]]:
        """Return (session_id, start_utc, end_utc, total_seconds, notes) rows, oldest failure first."""
        cur = self._conn.execute(
            "SELECT session_id, start_utc, end_utc, total_seconds, notes "
            "FROM failed_writes ORDER BY rowid ASC"
        )
        return cur.fetchall()

    def delete_failed_write(self, session_id: str) -> None:
        self._conn.execute("DELETE FROM failed_writes WHERE session_id = ?", (session_id,))
        self._conn.commit()

    # ---- live session event tracking (live MQTT mode) ----

    def append_live_event(self, session_id: str, event_time_ms: int, state: str) -> None:
        self._conn.execute(
            "INSERT INTO live_session_events (session_id, event_time_ms, state) VALUES (?, ?, ?)",
            (session_id, event_time_ms, state),
        )
        self._conn.commit()

    def get_live_events(self, session_id: str) -> list[tuple[int, str]]:
        """Return (event_time_ms, state) rows for a session, oldest first."""
        cur = self._conn.execute(
            "SELECT event_time_ms, state FROM live_session_events WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        )
        return cur.fetchall()

    def clear_live_events(self, session_id: str) -> None:
        self._conn.execute("DELETE FROM live_session_events WHERE session_id = ?", (session_id,))
        self._conn.commit()

    def open_live_session_ids(self) -> list[str]:
        """Distinct session_ids currently being tracked, oldest-opened first."""
        cur = self._conn.execute(
            "SELECT session_id FROM live_session_events GROUP BY session_id ORDER BY MIN(id)"
        )
        return [row[0] for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
