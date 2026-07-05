"""Reconstructs completed SNOO sleep sessions from live MQTT push events.

Pure logic, no network/Firestore I/O - sync/runner.py wires this to
python-snoo's live subscription and to Huckleberry writes.

IMPORTANT: python-snoo's SnooStateMachine.is_active_session is always True
after deserialization (mashumaro coerces the device's literal "false" string
into a truthy Python bool) - do not read it. session_id == "0" is the
reliable inactive signal, matching sync/snoo_source.py's REST-based
SnooDeviceState convention.
"""

import logging
from datetime import datetime, timezone

from python_snoo.containers import SnooData, SnooEvents

from .dedupe import DedupeStore
from .snoo_source import (
    SnooCompletedSession,
    aggregate_segment_durations,
    back_compute_start_ms,
    fmt_dur,
    format_session_notes,
)

log = logging.getLogger(__name__)

_ASLEEP_STATES = {"BASELINE", "WEANING_BASELINE"}
_SOOTHING_STATES = {"LEVEL1", "LEVEL2", "LEVEL3", "LEVEL4"}

# Grace period added to a stale (missed-close) session's last-recorded event
# timestamp when bounding its fabricated end time - see _close_open_sessions.
_STALE_SESSION_GRACE_MS = 10 * 60 * 1000  # 10 minutes


def _classify_state(state: str) -> tuple[str, str | None]:
    """Map a raw device state to (aggregation bucket label, optional individual
    sub-label). Soothing levels count toward the "soothing" summary total AND
    keep their own label (e.g. "Level2") so they also show as their own line."""
    s = (state or "").upper()
    if s in _ASLEEP_STATES:
        return "asleep", None
    if s in _SOOTHING_STATES:
        return "soothing", s.capitalize()
    return (state.capitalize() if state else "Unknown"), None


def _format_clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M")


def _soothing_episode_lines(events: list[tuple[int, str]], end_ms: int) -> list[str]:
    """One notes line per contiguous run of soothing states (LEVEL1-4), e.g.
    "- Soothing episode 1: 17:26-17:28 (Level1, 2m 17s)", so individual
    soothing bouts are visible alongside the aggregate Soothing/LevelN totals
    (which only show combined time across the whole session)."""
    lines: list[str] = []
    episode_start: int | None = None
    episode_levels: list[str] = []
    count = 0

    def close_episode(episode_end_ms: int) -> None:
        nonlocal count
        count += 1
        dur = (episode_end_ms - episode_start) / 1000
        lines.append(
            f"- Soothing episode {count}: {_format_clock(episode_start)}-{_format_clock(episode_end_ms)} "
            f"({'→'.join(episode_levels)}, {fmt_dur(dur)})"
        )

    for t_ms, state in events:
        is_soothing = state.upper() in _SOOTHING_STATES
        if is_soothing:
            if episode_start is None:
                episode_start = t_ms
                episode_levels = []
            label = state.capitalize()
            if not episode_levels or episode_levels[-1] != label:
                episode_levels.append(label)
        elif episode_start is not None:
            close_episode(t_ms)
            episode_start = None

    if episode_start is not None:
        close_episode(end_ms)

    return lines


def _infer_wake_reason(events: list[tuple[int, str]], closing_data: SnooData) -> str | None:
    """Best-effort guess at how the session ended, from the closing push event.
    Unvalidated against real sessions - expect to refine after real data lands."""
    last_state = events[-1][1].upper() if events else ""
    if closing_data.left_safety_clip == 0 or closing_data.right_safety_clip == 0:
        return "Picked up out of SNOO"
    if last_state == "TIMEOUT":
        return "Soothing timed out without settling"
    if last_state == "SUSPENDED":
        return "Session stopped manually"
    if closing_data.event == SnooEvents.CRY:
        return "Ended after sustained crying"
    return None


class LiveSessionTracker:
    """Tracks in-progress SNOO sessions across live push events, persisting
    each state transition to SQLite as it arrives so a process restart
    mid-session resumes from the persisted events instead of losing them."""

    def __init__(self, store: DedupeStore, min_session_seconds: float) -> None:
        self._store = store
        self._min_session_seconds = min_session_seconds

    def handle_event(self, data: SnooData) -> list[SnooCompletedSession]:
        session_id = str(data.state_machine.session_id)
        if session_id in ("0", ""):
            return self._close_open_sessions(data)

        state = str(data.state_machine.state)
        existing = self._store.get_live_events(session_id)
        if not existing:
            # Note: if this is actually a mid-session seed (process started or
            # reconnected while a session was already in progress), the entire
            # pre-seed span gets attributed to whatever single state is present
            # at seed time - an approximation, since we have no record of any
            # state transitions that happened before we started observing.
            # Steady-state sessions that begin while the process is already
            # running and connected are unaffected.
            start_ms = back_compute_start_ms(
                data.event_time_ms, data.state_machine.since_session_start_ms
            )
            if start_ms is None:
                start_ms = data.event_time_ms
            self._store.append_live_event(session_id, start_ms, state)
            log.info(
                "Tracking new live SNOO session %s (started %s)",
                session_id,
                datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
            )
        else:
            self._store.append_live_event(session_id, data.event_time_ms, state)
        return []

    def _close_open_sessions(self, closing_data: SnooData) -> list[SnooCompletedSession]:
        completed: list[SnooCompletedSession] = []
        open_ids = self._store.open_live_session_ids()
        for i, session_id in enumerate(open_ids):
            events = self._store.get_live_events(session_id)
            self._store.clear_live_events(session_id)

            if not events:
                log.warning("Live session %s had no recorded events, discarding.", session_id)
                continue

            start_ms = events[0][0]
            # open_live_session_ids() returns oldest-opened-first, so only the
            # LAST entry can plausibly be the session that actually triggered
            # this closing event. Any earlier open session_ids only exist
            # because their real close was missed (e.g. during a connection
            # outage covered by the resubscribe watchdog) - attributing this
            # closing event's timestamp to them could fabricate a wildly wrong,
            # multi-hour-long session. Instead, bound a stale session's end
            # time at its own last-recorded event timestamp plus a grace
            # period, producing a plausible-but-imperfect duration rather than
            # a bogus one.
            is_most_recent = (i == len(open_ids) - 1)
            if is_most_recent:
                end_ms = closing_data.event_time_ms
            else:
                last_recorded_ms = events[-1][0]
                end_ms = min(closing_data.event_time_ms, last_recorded_ms + _STALE_SESSION_GRACE_MS)
                log.warning(
                    "Live session %s appears stale (missed its real close, likely "
                    "during a connection outage) - closing at last-seen time + grace "
                    "instead of the current closing event's timestamp.",
                    session_id,
                )

            total_seconds = (end_ms - start_ms) / 1000
            if total_seconds < self._min_session_seconds:
                log.info("Live session %s too short (%.0fs) - discarding.", session_id, total_seconds)
                continue

            segments: list[tuple[str, float]] = []
            for j, (t_ms, state) in enumerate(events):
                next_ms = events[j + 1][0] if j + 1 < len(events) else end_ms
                dur = (next_ms - t_ms) / 1000
                bucket_label, sub_label = _classify_state(state)
                segments.append((bucket_label, dur))
                if sub_label:
                    segments.append((sub_label, dur))

            asleep_s, soothing_s, other = aggregate_segment_durations(segments)
            episode_lines = _soothing_episode_lines(events, end_ms)
            wake_reason = _infer_wake_reason(events, closing_data)
            extra_lines = episode_lines + ([f"- Ended: {wake_reason}"] if wake_reason else [])
            notes = format_session_notes(asleep_s, soothing_s, other, extra_lines=extra_lines or None)

            completed.append(SnooCompletedSession(
                session_id=session_id,
                start=datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc),
                end=datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc),
                total_seconds=total_seconds,
                notes=notes,
            ))
        return completed
