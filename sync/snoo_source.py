"""SNOO data source: fetch completed sleep sessions from history API."""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from python_snoo.containers import SnooData, SnooDevice
from python_snoo.snoo import Snoo

from .models import SnooCompletedSession

log = logging.getLogger(__name__)

BABIES_URL = "https://api-us-east-1-prod.happiestbaby.com/us/me/v10/babies"
SLEEP_URL = "https://api-us-east-1-prod.happiestbaby.com/ss/me/v11/babies/{baby_id}/sessions/daily"
DEVICES_URL = "https://api-us-east-1-prod.happiestbaby.com/hds/me/v11/devices"

# SNOO's "daily" sessions endpoint groups sessions starting at 6:00 AM local time.
_DAILY_WINDOW_START_HOUR = 6

_START_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


def _parse_start_time(raw: str) -> datetime | None:
    """Parse a SNOO startTime string, tolerating with/without fractional seconds."""
    for fmt in _START_TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


@dataclass
class SnooDeviceState:
    """Current live activity state, used in non-premium mode (no history endpoint access)."""

    session_id: str
    is_active: bool
    event_time_ms: int  # timestamp of the most recent state change on the device
    since_session_start_ms: int  # ms since session start; -1 when inactive

    @property
    def session_start_ms(self) -> int | None:
        """Back-compute session start from device-reported elapsed time."""
        if self.is_active and self.since_session_start_ms >= 0:
            return self.event_time_ms - self.since_session_start_ms
        return None

    @property
    def session_start(self) -> datetime | None:
        ms = self.session_start_ms
        if ms is None:
            return None
        return datetime.fromtimestamp(ms / 1000, tz=UTC)

    @property
    def event_time(self) -> datetime:
        return datetime.fromtimestamp(self.event_time_ms / 1000, tz=UTC)


def back_compute_start_ms(event_time_ms: int, since_session_start_ms: int | None) -> int | None:
    """Back-compute when a session began from a device event's timestamp and its
    reported elapsed-since-start. Returns None if the device hasn't reported a
    valid elapsed value (e.g. -1, meaning no session in progress)."""
    if since_session_start_ms is None or since_session_start_ms < 0:
        return None
    return event_time_ms - since_session_start_ms


def aggregate_segment_durations(
    segments: list[tuple[str, float]]
) -> tuple[float, float, dict[str, float]]:
    """segments: (type_label, duration_seconds) pairs. Returns
    (asleep_seconds, soothing_seconds, other_by_label), where `other` is
    exclusive of whatever was already counted as asleep/soothing.

    Note: a composite label like "asleep-baseline" counts toward the asleep
    total (substring match) but is NOT also shown as its own "other" entry -
    a narrow, deliberate behavior difference from the pre-refactor premium
    code, which double-counted such labels into both the total and their own
    notes line. Accepted as out of scope: unconfirmed to occur with any real
    premium data, and preserving it would require polluting this shared,
    live-mode-consumed aggregator with premium-only display quirks.
    """
    asleep = 0.0
    soothing = 0.0
    other: dict[str, float] = defaultdict(float)
    for seg_type, dur in segments:
        if not isinstance(dur, (int, float)):
            continue
        ll = (seg_type or "").lower()
        if "soothing" in ll:
            soothing += dur
        elif "asleep" in ll:
            asleep += dur
        elif seg_type:
            other[seg_type] += dur
    return asleep, soothing, dict(other)


def fmt_dur(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    parts = []
    if h > 0:
        parts.append(f"{h}h")
    if m > 0:
        parts.append(f"{m}m")
    if s > 0 or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def format_session_notes(
    asleep_s: float,
    soothing_s: float,
    other: dict[str, float],
    extra_lines: list[str] | None = None,
    *,
    detailed: bool = True,
    total_seconds: float | None = None,
    soothing_episode_count: int | None = None,
) -> str:
    """Render a SNOO session's Huckleberry notes field.

    detailed=True (default) reproduces the original Asleep/Soothing/per-level
    breakdown byte-for-byte - premium mode's fetch_past_sessions never passes
    detailed=, so it keeps today's exact format with zero changes.

    detailed=False (live mode's NOTES_DETAIL=summary, the new live-mode default)
    renders a compact Total/Soothing(with episode count)/wake-reason summary.
    Requires total_seconds - the true wall-clock session length, NOT derivable
    as asleep_s + soothing_s + sum(other.values()): other may already double-count
    soothing-level durations that are also folded into soothing_s (see
    aggregate_segment_durations's per-level display labels) - callers must pass
    the real total explicitly.
    """
    if detailed:
        lines = [
            "SNOO Sleep Session Summary:",
            f"\n- Asleep: {fmt_dur(asleep_s)}",
            f"\n- Soothing: {fmt_dur(soothing_s)}",
        ]
        for label, dur in sorted(other.items()):
            lines.append(f"- {label.capitalize()}: {fmt_dur(dur)}")
        if extra_lines:
            lines.extend(extra_lines)
        return "\n".join(lines)

    if total_seconds is None:
        raise ValueError("total_seconds is required when detailed=False")

    soothing_line = f"- Soothing: {fmt_dur(soothing_s)}"
    if soothing_episode_count:
        noun = "episode" if soothing_episode_count == 1 else "episodes"
        soothing_line += f" ({soothing_episode_count} {noun})"

    lines = [
        "SNOO Sleep Session Summary:",
        f"- Total: {fmt_dur(total_seconds)}",
        soothing_line,
    ]
    if extra_lines:
        lines.extend(extra_lines)
    return "\n".join(lines)


async def fetch_device_state(
    websession: aiohttp.ClientSession,
    username: str,
    password: str,
) -> SnooDeviceState:
    """Authenticate and return the current SNOO activity state.

    Used in non-premium mode: the sessions/daily history endpoint requires a SNOO Premium
    subscription and returns no data without one, so completed sessions must instead be
    reconstructed by polling live device state and tracking is_active_session transitions
    across successive polls (see runner.py).
    """
    snoo = Snoo(username, password, websession)
    await snoo.authorize()
    hdrs = snoo.generate_snoo_auth_headers(snoo.tokens.aws_id)

    async with websession.get(DEVICES_URL, headers=hdrs) as r:
        r.raise_for_status()
        data = await r.json()

    devices = data.get("snoo", [])
    if not devices:
        raise RuntimeError("No SNOO devices found on this account")

    activity = devices[0].get("activityState", {})
    sm = activity.get("state_machine", {})

    # is_active_session comes back as the string "true"/"false", not a bool
    is_active = str(sm.get("is_active_session", "false")).lower() == "true"
    session_id = str(sm.get("session_id", "0"))
    event_time_ms = int(activity.get("event_time_ms", 0))
    since_start = int(sm.get("since_session_start_ms", -1))

    state = SnooDeviceState(
        session_id=session_id,
        is_active=is_active,
        event_time_ms=event_time_ms,
        since_session_start_ms=since_start,
    )

    # Cancel the background reauth task - our aiohttp session is short-lived
    # and will be closed before the 175-min reauth window fires.
    if snoo.reauth_task:
        snoo.reauth_task.cancel()
        snoo.reauth_task = None

    log.info(
        "Device state: is_active=%s  session_id=%s  event_time=%s  since_start=%ds",
        is_active,
        session_id,
        state.event_time.isoformat(),
        since_start // 1000 if since_start > 0 else -1,
    )
    return state


async def start_live_subscription(
    websession: aiohttp.ClientSession,
    username: str,
    password: str,
    on_message: Callable[[SnooData], None],
) -> tuple[Snoo, SnooDevice]:
    """Authenticate, resolve the account's first SNOO device, and start a
    persistent AWS IoT MQTT subscription delivering live state-transition
    events - the same mechanism the official Home Assistant SNOO integration
    uses (see homeassistant/components/snoo/coordinator.py upstream).

    Returns (snoo, device) so the caller can run a heartbeat/resubscribe
    watchdog (checking snoo._mqtt_tasks[device.serialNumber] and calling
    snoo.start_subscribe(device, on_message) again if it died) and keep the
    Snoo instance alive for its automatic token-refresh/resubscription.
    """
    snoo = Snoo(username, password, websession)
    await snoo.authorize()

    devices = await snoo.get_devices()
    if not devices:
        raise RuntimeError("No SNOO devices found on this account")
    device = devices[0]
    log.info("Live mode tracking device %s (%s)", device.serialNumber, device.name)

    snoo.start_subscribe(device, on_message)
    try:
        await snoo.get_status(device)
    except Exception:
        log.warning(
            "Initial device status request for %s failed or timed out; the live "
            "subscription is still active and will pick up state on the next "
            "real transition.",
            device.serialNumber,
            exc_info=True,
        )
    return snoo, device


def get_subscription_task(snoo: Snoo, device: SnooDevice) -> asyncio.Task | None:
    """Return the live MQTT subscription task for `device`, or None if none exists.

    Wraps python-snoo's private `_mqtt_tasks` map so callers (the runner's
    heartbeat watchdog) don't reach into the library's internals directly."""
    return snoo._mqtt_tasks.get(device.serialNumber)


def resubscribe(snoo: Snoo, device: SnooDevice, on_message: Callable[[SnooData], None]) -> None:
    """Restart the live MQTT subscription for `device`.

    `start_subscribe` is synchronous in python-snoo - it schedules an internal
    `asyncio.create_task(subscribe_mqtt(...))` and returns. Wrapped here so the
    private-method access lives in one place (see get_subscription_task)."""
    snoo.start_subscribe(device, on_message)


async def fetch_past_sessions(
    websession: aiohttp.ClientSession,
    username: str,
    password: str,
    timezone_str: str,
    days: int = 1,
    baby_id_override: str | None = None,
) -> list[SnooCompletedSession]:
    """Authenticate and return consolidated completed sleep sessions from SNOO history."""
    snoo = Snoo(username, password, websession)
    await snoo.authorize()
    hdrs = snoo.generate_snoo_auth_headers(snoo.tokens.aws_id)

    # 1. Resolve baby_id
    if baby_id_override:
        baby_id = baby_id_override
        log.info("Using configured SNOO baby ID override: %s", baby_id)
    else:
        async with websession.get(BABIES_URL, headers=hdrs) as r:
            r.raise_for_status()
            babies = await r.json()

        if not babies:
            raise RuntimeError("No babies found on this SNOO account")
        baby_id = babies[0]["_id"]
        log.info("Resolved SNOO baby ID: %s", baby_id)

    # 2. Compute date range starting at 6:00 AM in local timezone (standard daily window start)
    tz = ZoneInfo(timezone_str)
    now_local = datetime.now(tz)
    start_date = (now_local - timedelta(days=days)).replace(
        hour=_DAILY_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )

    # 3. Fetch sleep sessions from daily endpoint day-by-day
    all_entries = []
    current_date = start_date

    while current_date.date() <= now_local.date():
        start_time_str = current_date.strftime("%Y-%m-%d %H:%M:%S.000")
        params = {
            "detailedLevels": "true",
            "levels": "true",
            "startTime": start_time_str,
            "timezone": timezone_str,
        }

        log.debug("Fetching SNOO history starting at %s", start_time_str)
        async with websession.get(SLEEP_URL.format(baby_id=baby_id), headers=hdrs, params=params) as r:
            r.raise_for_status()
            data = await r.json()

        if isinstance(data, dict) and "levels" in data:
            sessions = data["levels"]
        elif isinstance(data, list):
            sessions = data
        else:
            sessions = []

        all_entries.extend(sessions)
        current_date += timedelta(days=1)

    # Cancel the background reauth task
    if snoo.reauth_task:
        snoo.reauth_task.cancel()
        snoo.reauth_task = None

    # 4. Group and consolidate by sessionId
    sessions_map = defaultdict(list)
    for entry in all_entries:
        session_id = entry.get("sessionId")
        if session_id:
            sessions_map[session_id].append(entry)

    completed_sessions = []
    for session_id, segments in sessions_map.items():
        # Chronological sort
        try:
            segments.sort(key=lambda x: x.get("startTime", ""))
        except Exception:
            log.warning("Failed to sort segments for session %s", session_id, exc_info=True)

        start_times: list[datetime] = []
        for seg in segments:
            st = seg.get("startTime")
            if not st:
                continue
            parsed = _parse_start_time(st)
            if parsed is not None:
                start_times.append(parsed)

        if not start_times:
            continue

        # Earliest start (naive local time → attach zone → convert to UTC)
        start_dt_utc = min(start_times).replace(tzinfo=tz).astimezone(UTC)

        # Aggregate durations once via the shared helper. `total` is the sum of
        # every numeric segment duration; segments with missing/non-numeric
        # stateDuration are silently skipped by aggregate_segment_durations, so
        # if any are encountered the computed end time will be a lower bound.
        seg_pairs = [(seg.get("type", ""), seg.get("stateDuration")) for seg in segments]
        asleep_duration, soothing_duration, other_states = aggregate_segment_durations(seg_pairs)
        total_duration = asleep_duration + soothing_duration + sum(other_states.values())

        # Warn if any segment had a non-numeric duration - the end time derived
        # from total_duration would then be a lower bound (see aggregate_segment_durations).
        missing_dur = sum(
            1 for _, d in seg_pairs if not isinstance(d, (int, float))
        )
        if missing_dur:
            log.warning(
                "Session %s has %d segment(s) with non-numeric stateDuration; "
                "computed end time may be understated.",
                session_id, missing_dur,
            )

        end_dt_utc = start_dt_utc + timedelta(seconds=total_duration)
        notes = format_session_notes(asleep_duration, soothing_duration, other_states)

        completed_sessions.append(SnooCompletedSession(
            session_id=session_id,
            start=start_dt_utc,
            end=end_dt_utc,
            total_seconds=total_duration,
            notes=notes,
        ))

    # Sort completed sessions chronologically by start time
    completed_sessions.sort(key=lambda x: x.start)
    log.info("Fetched %d completed sessions from SNOO history", len(completed_sessions))
    return completed_sessions
