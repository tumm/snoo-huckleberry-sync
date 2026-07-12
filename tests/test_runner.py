"""Tests for sync.runner's retry helper."""

import asyncio

import pytest

from sync.runner import _retry_with_backoff


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
