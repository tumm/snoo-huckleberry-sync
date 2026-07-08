"""Tests for the pure formatting/aggregation helpers in sync.snoo_source."""

from sync.snoo_source import (
    aggregate_segment_durations,
    fmt_dur,
    format_session_notes,
)


class TestFmtDur:
    def test_zero(self):
        assert fmt_dur(0) == "0s"

    def test_seconds_only(self):
        assert fmt_dur(45) == "45s"

    def test_minutes_and_seconds(self):
        assert fmt_dur(125) == "2m 5s"

    def test_hours_minutes_seconds(self):
        assert fmt_dur(3661) == "1h 1m 1s"

    def test_exact_hour(self):
        assert fmt_dur(3600) == "1h"

    def test_exact_minute(self):
        assert fmt_dur(60) == "1m"


class TestAggregateSegmentDurations:
    def test_asleep_and_soothing(self):
        segments = [("asleep", 300.0), ("soothing", 120.0), ("asleep", 600.0)]
        asleep, soothing, other = aggregate_segment_durations(segments)
        assert asleep == 900.0
        assert soothing == 120.0
        assert other == {}

    def test_other_states_excluded_from_asleep_soothing(self):
        segments = [("asleep", 300.0), ("TIMEOUT", 30.0), ("soothing", 60.0)]
        asleep, soothing, other = aggregate_segment_durations(segments)
        assert asleep == 300.0
        assert soothing == 60.0
        assert other == {"TIMEOUT": 30.0}

    def test_substring_matching(self):
        segments = [("asleep-baseline", 300.0), ("LEVEL1-soothing", 120.0)]
        asleep, soothing, other = aggregate_segment_durations(segments)
        assert asleep == 300.0
        assert soothing == 120.0
        assert other == {}

    def test_non_numeric_skipped(self):
        segments = [("asleep", 300.0), ("soothing", None), ("asleep", 60.0)]
        asleep, soothing, other = aggregate_segment_durations(segments)
        assert asleep == 360.0
        assert soothing == 0.0

    def test_empty_label_skipped_from_other(self):
        segments = [("", 100.0), ("asleep", 200.0)]
        asleep, soothing, other = aggregate_segment_durations(segments)
        assert asleep == 200.0
        assert other == {}

    def test_empty_list(self):
        asleep, soothing, other = aggregate_segment_durations([])
        assert asleep == 0.0
        assert soothing == 0.0
        assert other == {}


class TestFormatSessionNotes:
    def test_detailed_basic(self):
        notes = format_session_notes(300.0, 120.0, {"TIMEOUT": 30.0})
        assert "Asleep: 5m" in notes
        assert "Soothing: 2m" in notes
        assert "Timeout: 30s" in notes

    def test_detailed_with_extra_lines(self):
        notes = format_session_notes(
            300.0, 120.0, {}, extra_lines=["- Ended: Picked up out of SNOO"]
        )
        assert "Picked up out of SNOO" in notes

    def test_summary_requires_total_seconds(self):
        import pytest
        with pytest.raises(ValueError):
            format_session_notes(300.0, 120.0, {}, detailed=False)

    def test_summary_with_episode_count(self):
        notes = format_session_notes(
            300.0, 120.0, {},
            detailed=False,
            total_seconds=450.0,
            soothing_episode_count=2,
        )
        assert "Total: 7m 30s" in notes
        assert "Soothing: 2m (2 episodes)" in notes

    def test_summary_single_episode_noun(self):
        notes = format_session_notes(
            300.0, 60.0, {},
            detailed=False,
            total_seconds=360.0,
            soothing_episode_count=1,
        )
        assert "1 episode" in notes
