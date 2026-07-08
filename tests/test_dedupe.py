"""Tests for DedupeStore - the SQLite-backed idempotency store."""

import threading
from datetime import UTC, datetime

import pytest

from sync.dedupe import DedupeStore


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test.sqlite"
    s = DedupeStore(str(db))
    yield s
    s.close()


class TestWrittenSessions:
    def test_seen_returns_false_for_unknown(self, store):
        assert store.seen("abc") is False

    def test_mark_makes_seen_true(self, store):
        start = datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime(2024, 1, 1, hour=1, tzinfo=UTC)
        store.mark("abc", start, end)
        assert store.seen("abc") is True

    def test_mark_is_idempotent(self, store):
        start = datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime(2024, 1, 1, hour=1, tzinfo=UTC)
        store.mark("abc", start, end)
        store.mark("abc", start, end)  # should not raise
        assert store.seen("abc") is True


class TestActiveSessions:
    def test_record_and_get(self, store):
        store.record_active_session("s1", 1000, 2000)
        active = store.get_active_sessions()
        assert len(active) == 1
        assert active[0] == ("s1", 1000, 2000)

    def test_update_last_event(self, store):
        store.record_active_session("s1", 1000, 2000)
        store.update_active_session_event("s1", 3000)
        active = store.get_active_sessions()
        assert active[0][2] == 3000

    def test_close_removes(self, store):
        store.record_active_session("s1", 1000, 2000)
        store.close_active_session("s1")
        assert store.get_active_sessions() == []

    def test_record_is_idempotent(self, store):
        store.record_active_session("s1", 1000, 2000)
        store.record_active_session("s1", 5000, 6000)  # INSERT OR IGNORE
        active = store.get_active_sessions()
        assert active[0] == ("s1", 1000, 2000)  # original kept


class TestLiveEvents:
    def test_append_and_get(self, store):
        store.append_live_event("s1", 1000, "BASELINE")
        store.append_live_event("s1", 2000, "LEVEL1")
        events = store.get_live_events("s1")
        assert events == [(1000, "BASELINE"), (2000, "LEVEL1")]

    def test_get_returns_oldest_first(self, store):
        store.append_live_event("s1", 3000, "LEVEL2")
        store.append_live_event("s1", 1000, "BASELINE")
        store.append_live_event("s1", 2000, "LEVEL1")
        events = store.get_live_events("s1")
        assert events[0] == (3000, "LEVEL2")  # insertion order, not timestamp

    def test_clear(self, store):
        store.append_live_event("s1", 1000, "BASELINE")
        store.clear_live_events("s1")
        assert store.get_live_events("s1") == []

    def test_open_session_ids(self, store):
        store.append_live_event("s1", 1000, "BASELINE")
        store.append_live_event("s2", 2000, "BASELINE")
        ids = store.open_live_session_ids()
        assert ids == ["s1", "s2"]


class TestConcurrency:
    def test_concurrent_writes_dont_corrupt(self, tmp_path):
        db = tmp_path / "concurrent.sqlite"
        store = DedupeStore(str(db))
        start = datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime(2024, 1, 1, hour=1, tzinfo=UTC)

        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            try:
                for i in range(50):
                    store.mark(f"t{thread_id}-s{i}", start, end)
                    store.seen(f"t{thread_id}-s{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        store.close()
        assert errors == []
