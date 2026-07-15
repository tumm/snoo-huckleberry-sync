"""Tests for sync.runner's retry helper and failed-write outbox."""

import asyncio
from datetime import UTC, datetime

import pytest

import sync.runner as runner
from sync.dedupe import DedupeStore
from sync.models import SnooCompletedSession
from sync.runner import _retry_failed_writes, _retry_with_backoff, _write_one_live_session


@pytest.fixture
def store(tmp_path):
    s = DedupeStore(str(tmp_path / "test.sqlite"))
    yield s
    s.close()


@pytest.fixture
def sess():
    return SnooCompletedSession(
        session_id="s1",
        start=datetime(2026, 7, 14, 21, 53, 12, tzinfo=UTC),
        end=datetime(2026, 7, 15, 0, 18, 26, tzinfo=UTC),
        total_seconds=8714.0,
        notes="SNOO Sleep Session Summary:\n- Total: 2h 25m 14s",
    )


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(runner, "_WRITE_RETRY_BASE_DELAY", 0.0)


class TestWriteOneLiveSessionOutbox:
    def test_failed_write_is_saved_to_outbox(self, store, sess, monkeypatch):
        async def always_fails(hb, child_uid, s):
            raise RuntimeError("429 simulated")

        monkeypatch.setattr(runner, "write_sleep_interval", always_fails)
        asyncio.run(_write_one_live_session(store, hb=None, child_uid="c1", sess=sess, dry=False))

        rows = store.get_failed_writes()
        assert [r[0] for r in rows] == ["s1"]
        assert rows[0][1] == sess.start.isoformat()
        assert rows[0][2] == sess.end.isoformat()
        assert rows[0][3] == sess.total_seconds
        assert rows[0][4] == sess.notes

    def test_successful_write_not_saved_to_outbox(self, store, sess, monkeypatch):
        async def ok(hb, child_uid, s):
            return None

        monkeypatch.setattr(runner, "write_sleep_interval", ok)
        asyncio.run(_write_one_live_session(store, hb=None, child_uid="c1", sess=sess, dry=False))

        assert store.get_failed_writes() == []
        assert store.seen("s1") is True


class TestRetryFailedWrites:
    def test_pending_session_written_and_removed(self, store, sess, monkeypatch):
        store.save_failed_write(sess.session_id, sess.start, sess.end, sess.total_seconds, sess.notes)
        written = []

        async def ok(hb, child_uid, s):
            written.append(s)

        monkeypatch.setattr(runner, "write_sleep_interval", ok)
        asyncio.run(_retry_failed_writes(store, hb=None, child_uid="c1"))

        assert store.get_failed_writes() == []
        assert store.seen("s1") is True
        assert len(written) == 1
        assert written[0].session_id == "s1"
        assert written[0].start == sess.start
        assert written[0].end == sess.end
        assert written[0].total_seconds == sess.total_seconds
        assert written[0].notes == sess.notes

    def test_still_failing_session_kept_for_next_retry(self, store, sess, monkeypatch):
        store.save_failed_write(sess.session_id, sess.start, sess.end, sess.total_seconds, sess.notes)

        async def always_fails(hb, child_uid, s):
            raise RuntimeError("429 still throttled")

        monkeypatch.setattr(runner, "write_sleep_interval", always_fails)
        asyncio.run(_retry_failed_writes(store, hb=None, child_uid="c1"))

        assert [r[0] for r in store.get_failed_writes()] == ["s1"]
        assert store.seen("s1") is False

    def test_already_written_session_just_removed(self, store, sess, monkeypatch):
        store.save_failed_write(sess.session_id, sess.start, sess.end, sess.total_seconds, sess.notes)
        store.mark(sess.session_id, sess.start, sess.end)

        async def must_not_be_called(hb, child_uid, s):
            raise AssertionError("write_sleep_interval should not run for a seen session")

        monkeypatch.setattr(runner, "write_sleep_interval", must_not_be_called)
        asyncio.run(_retry_failed_writes(store, hb=None, child_uid="c1"))

        assert store.get_failed_writes() == []

    def test_incident_recovery_cycle(self, store, sess, monkeypatch):
        """The 2026-07-15 incident: securetoken 429 exhausts all write attempts,
        session is queued instead of lost, next heartbeat retry writes it."""

        async def throttled(hb, child_uid, s):
            raise RuntimeError("429, message='Too Many Requests'")

        monkeypatch.setattr(runner, "write_sleep_interval", throttled)
        asyncio.run(_write_one_live_session(store, hb=None, child_uid="c1", sess=sess, dry=False))
        assert store.seen("s1") is False
        assert [r[0] for r in store.get_failed_writes()] == ["s1"]

        async def ok(hb, child_uid, s):
            return None

        monkeypatch.setattr(runner, "write_sleep_interval", ok)
        asyncio.run(_retry_failed_writes(store, hb=None, child_uid="c1"))
        assert store.seen("s1") is True
        assert store.get_failed_writes() == []

    def test_noop_when_outbox_empty(self, store, monkeypatch):
        async def must_not_be_called(hb, child_uid, s):
            raise AssertionError("no writes expected")

        monkeypatch.setattr(runner, "write_sleep_interval", must_not_be_called)
        asyncio.run(_retry_failed_writes(store, hb=None, child_uid="c1"))

        assert store.get_failed_writes() == []


class TestRetryWithBackoff:
    def test_succeeds_on_first_attempt_no_retry(self):
        calls = []

        async def ok():
            calls.append(1)
            return "done"

        assert asyncio.run(_retry_with_backoff(ok, attempts=3, base_delay=0)) == "done"
        assert len(calls) == 1

    def test_retries_transient_failure_then_succeeds(self):
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("transient")
            return "done"

        assert asyncio.run(_retry_with_backoff(flaky, attempts=3, base_delay=0)) == "done"
        assert len(calls) == 2

    def test_raises_after_exhausting_attempts(self):
        calls = []

        async def always_fails():
            calls.append(1)
            raise RuntimeError("permanent")

        async def _run():
            with pytest.raises(RuntimeError, match="permanent"):
                await _retry_with_backoff(always_fails, attempts=3, base_delay=0)

        asyncio.run(_run())
        assert len(calls) == 3

    def test_should_abort_checked_before_every_attempt(self):
        calls = []

        async def flaky():
            calls.append(1)
            raise RuntimeError("transient")

        def should_abort():
            return len(calls) >= 1  # abort starting on the 2nd attempt

        async def _run():
            result = await _retry_with_backoff(
                flaky, attempts=5, base_delay=0, should_abort=should_abort
            )
            assert result is None

        asyncio.run(_run())
        assert len(calls) == 1  # 2nd attempt never ran

    def test_on_retry_called_with_attempt_delay_and_exception(self):
        recorded = []

        async def flaky():
            if len(recorded) < 1:
                raise RuntimeError("boom")
            return "done"

        def on_retry(attempt, delay, exc):
            recorded.append((attempt, delay, str(exc)))

        result = asyncio.run(
            _retry_with_backoff(flaky, attempts=3, base_delay=0, on_retry=on_retry)
        )
        assert result == "done"
        assert recorded == [(1, 0, "boom")]
