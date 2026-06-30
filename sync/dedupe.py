"""SQLite-backed idempotency store.

written_sessions - sessions already synced to Huckleberry (permanent).
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS written_sessions (
    session_id   TEXT PRIMARY KEY,
    start_utc    TEXT NOT NULL,
    end_utc      TEXT NOT NULL,
    written_at   TEXT NOT NULL
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
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT OR IGNORE INTO written_sessions (session_id, start_utc, end_utc, written_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, start.isoformat(), end.isoformat(), now),
        )
        self._conn.commit()
        log.debug("Marked session %s as written", session_id)

    def close(self) -> None:
        self._conn.close()
