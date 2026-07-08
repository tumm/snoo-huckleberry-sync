"""Tests for LiveSessionTracker - pure session-reconstruction logic."""

from datetime import UTC, datetime

import pytest
from python_snoo.containers import SnooEvents

from sync.dedupe import DedupeStore
from sync.live_source import (
    LiveSessionTracker,
    _classify_state,
    _find_soothing_episodes,
    _infer_wake_reason,
)
from tests.conftest import make_event


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test.sqlite"
    s = DedupeStore(str(db))
    yield s
    s.close()


@pytest.fixture
def tracker(store):
    return LiveSessionTracker(store, min_session_seconds=60.0, notes_detail="summary")


class TestClassifyState:
    def test_baseline_is_asleep(self):
        bucket, sub = _classify_state("BASELINE")
        assert bucket == "asleep"
        assert sub is None

    def test_weaning_baseline_is_asleep(self):
        bucket, sub = _classify_state("WEANING_BASELINE")
        assert bucket == "asleep"
        assert sub is None

    def test_level1_is_soothing_with_sublabel(self):
        bucket, sub = _classify_state("LEVEL1")
        assert bucket == "soothing"
        assert sub == "Level1"

    def test_level4_is_soothing_with_sublabel(self):
        bucket, sub = _classify_state("LEVEL4")
        assert bucket == "soothing"
        assert sub == "Level4"

    def test_unknown_state_capitalized(self):
        bucket, sub = _classify_state("TIMEOUT")
        assert bucket == "Timeout"
        assert sub is None

    def test_empty_state(self):
        bucket, sub = _classify_state("")
        assert bucket == "Unknown"
        assert sub is None


class TestFindSoothingEpisodes:
    def test_no_soothing(self):
        events = [(0, "BASELINE"), (300000, "ASLEEP")]
        episodes = _find_soothing_episodes(events, end_ms=600000)
        assert episodes == []

    def test_single_episode(self):
        events = [(0, "BASELINE"), (300000, "LEVEL1"), (360000, "BASELINE")]
        episodes = _find_soothing_episodes(events, end_ms=600000)
        assert len(episodes) == 1
        start_ms, end_ms, levels = episodes[0]
        assert start_ms == 300000
        assert end_ms == 360000
        assert levels == ["Level1"]

    def test_multiple_episodes(self):
        events = [
            (0, "BASELINE"),
            (300000, "LEVEL1"),
            (360000, "BASELINE"),
            (480000, "LEVEL2"),
            (540000, "BASELINE"),
        ]
        episodes = _find_soothing_episodes(events, end_ms=600000)
        assert len(episodes) == 2
        assert episodes[0][0] == 300000
        assert episodes[1][0] == 480000

    def test_episode_at_end(self):
        events = [(0, "BASELINE"), (300000, "LEVEL1")]
        episodes = _find_soothing_episodes(events, end_ms=600000)
        assert len(episodes) == 1
        start_ms, end_ms, levels = episodes[0]
        assert end_ms == 600000

    def test_level_transitions_within_episode(self):
        events = [(0, "LEVEL1"), (60000, "LEVEL2"), (120000, "BASELINE")]
        episodes = _find_soothing_episodes(events, end_ms=180000)
        assert len(episodes) == 1
        _, _, levels = episodes[0]
        assert levels == ["Level1", "Level2"]


class TestInferWakeReason:
    def test_safety_clip_release(self):
        events = [(0, "BASELINE")]
        closing = make_event(left_safety_clip=0, right_safety_clip=1)
        assert _infer_wake_reason(events, closing) == "Picked up out of SNOO"

    def test_right_clip_release(self):
        events = [(0, "BASELINE")]
        closing = make_event(left_safety_clip=1, right_safety_clip=0)
        assert _infer_wake_reason(events, closing) == "Picked up out of SNOO"

    def test_timeout(self):
        events = [(0, "BASELINE"), (300000, "TIMEOUT")]
        closing = make_event(state="TIMEOUT")
        assert _infer_wake_reason(events, closing) == "Soothing timed out without settling"

    def test_suspended(self):
        events = [(0, "BASELINE"), (300000, "SUSPENDED")]
        closing = make_event(state="SUSPENDED")
        assert _infer_wake_reason(events, closing) == "Session stopped manually"

    def test_fuzzy_wakeup(self):
        events = [(0, "BASELINE"), (300000, "LEVEL2")]
        closing = make_event(state="LEVEL2")
        assert _infer_wake_reason(events, closing) == "Fuzzy wakeup"

    def test_cry_event(self):
        events = [(0, "BASELINE")]
        closing = make_event(event=SnooEvents.CRY)
        assert _infer_wake_reason(events, closing) == "Ended after sustained crying"

    def test_no_reason(self):
        events = [(0, "BASELINE")]
        closing = make_event(state="BASELINE")
        assert _infer_wake_reason(events, closing) is None


class TestLiveSessionTrackerHandleEvent:
    def test_new_session_seeds_first_event(self, tracker, store):
        event = make_event(
            session_id="abc",
            state="BASELINE",
            event_time_ms=1_000_000,
            since_session_start_ms=60_000,
        )
        result = tracker.handle_event(event)
        assert result == []
        events = store.get_live_events("abc")
        assert len(events) == 1
        # Seed event back-computes start time: event_time - since_start
        assert events[0][0] == 940_000  # 1_000_000 - 60_000

    def test_subsequent_event_appends(self, tracker, store):
        seed = make_event(
            session_id="abc",
            state="BASELINE",
            event_time_ms=1_000_000,
            since_session_start_ms=0,
        )
        tracker.handle_event(seed)

        next_event = make_event(
            session_id="abc",
            state="LEVEL1",
            event_time_ms=1_060_000,
            since_session_start_ms=60_000,
        )
        result = tracker.handle_event(next_event)
        assert result == []
        events = store.get_live_events("abc")
        assert len(events) == 2
        assert events[1] == (1_060_000, "LEVEL1")

    def test_duplicate_event_dropped(self, tracker, store):
        event = make_event(
            session_id="abc",
            state="BASELINE",
            event_time_ms=1_000_000,
            since_session_start_ms=0,
        )
        tracker.handle_event(event)
        # Send the same event again (e.g. after reconnect)
        result = tracker.handle_event(event)
        assert result == []
        events = store.get_live_events("abc")
        assert len(events) == 1  # no duplicate

    def test_session_id_zero_closes_open_sessions(self, tracker, store):
        seed = make_event(
            session_id="abc",
            state="BASELINE",
            event_time_ms=1_000_000,
            since_session_start_ms=0,
        )
        tracker.handle_event(seed)

        close_event = make_event(
            session_id="0",
            state="ONLINE",
            event_time_ms=1_300_000,
        )
        result = tracker.handle_event(close_event)
        assert len(result) == 1
        sess = result[0]
        assert sess.session_id == "abc"
        assert sess.start == datetime.fromtimestamp(1000.0, tz=UTC)
        assert sess.end == datetime.fromtimestamp(1300.0, tz=UTC)
        assert sess.total_seconds == 300.0

    def test_short_session_discarded(self, tracker, store):
        seed = make_event(
            session_id="abc",
            state="BASELINE",
            event_time_ms=1_000_000,
            since_session_start_ms=0,
        )
        tracker.handle_event(seed)
        close_event = make_event(session_id="0", state="ONLINE", event_time_ms=1_030_000)
        result = tracker.handle_event(close_event)
        assert result == []  # 30s < 60s min

    def test_stale_session_bounded(self, tracker, store):
        old_seed = make_event(
            session_id="old",
            state="BASELINE",
            event_time_ms=1_000_000,
            since_session_start_ms=0,
        )
        tracker.handle_event(old_seed)
        recent_seed = make_event(
            session_id="recent",
            state="BASELINE",
            event_time_ms=2_000_000,
            since_session_start_ms=0,
        )
        tracker.handle_event(recent_seed)

        close_event = make_event(session_id="0", state="ONLINE", event_time_ms=2_100_000)
        result = tracker.handle_event(close_event)
        assert len(result) == 2
        # The stale session should be bounded by its last event + grace, not the
        # closing event's timestamp (2_100_000)
        old_sess = next(s for s in result if s.session_id == "old")
        # stale grace = 10 * 60 * 1000 = 600_000; last event was 1_000_000
        # end = min(2_100_000, 1_000_000 + 600_000) = 1_600_000
        assert old_sess.end == datetime.fromtimestamp(1600.0, tz=UTC)

    def test_summary_notes_format(self, tracker, store):
        seed = make_event(
            session_id="abc",
            state="BASELINE",
            event_time_ms=1_000_000,
            since_session_start_ms=0,
        )
        tracker.handle_event(seed)
        close_event = make_event(session_id="0", state="ONLINE", event_time_ms=1_300_000)
        result = tracker.handle_event(close_event)
        assert len(result) == 1
        notes = result[0].notes
        assert "Total: 5m" in notes
        assert "Asleep" in notes or "Soothing" in notes


class TestLiveSessionTrackerDetailed:
    def test_detailed_notes_format(self, store):
        tracker = LiveSessionTracker(store, min_session_seconds=0, notes_detail="detailed")
        seed = make_event(
            session_id="abc",
            state="BASELINE",
            event_time_ms=1_000_000,
            since_session_start_ms=0,
        )
        tracker.handle_event(seed)
        close_event = make_event(session_id="0", state="ONLINE", event_time_ms=1_300_000)
        result = tracker.handle_event(close_event)
        assert len(result) == 1
        notes = result[0].notes
        assert "Asleep:" in notes
        assert "Soothing:" in notes
