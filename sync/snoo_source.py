"""SNOO data source: fetch completed sleep sessions from history API."""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from python_snoo.snoo import Snoo

log = logging.getLogger(__name__)

BABIES_URL = "https://api-us-east-1-prod.happiestbaby.com/us/me/v10/babies"
SLEEP_URL = "https://api-us-east-1-prod.happiestbaby.com/ss/me/v11/babies/{baby_id}/sessions/daily"


@dataclass
class SnooCompletedSession:
    session_id: str
    start: datetime  # aware datetime in UTC
    end: datetime    # aware datetime in UTC
    total_seconds: float
    notes: str


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
    start_date = (now_local - timedelta(days=days)).replace(hour=6, minute=0, second=0, microsecond=0)

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

        start_times = []
        for seg in segments:
            st = seg.get("startTime")
            if st:
                try:
                    parsed = datetime.strptime(st, "%Y-%m-%d %H:%M:%S.%f")
                except Exception:
                    try:
                        parsed = datetime.strptime(st, "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        continue
                start_times.append((parsed, st))

        if not start_times:
            continue

        # Earliest start
        earliest_local_dt, _ = min(start_times)
        start_dt_utc = earliest_local_dt.replace(tzinfo=tz).astimezone(timezone.utc)

        total_duration = 0.0
        soothing_duration = 0.0
        asleep_duration = 0.0
        for seg in segments:
            dur = seg.get("stateDuration")
            if isinstance(dur, (int, float)):
                total_duration += dur
                seg_type = seg.get("type", "").lower()
                if "soothing" in seg_type:
                    soothing_duration += dur
                elif "asleep" in seg_type:
                    asleep_duration += dur

        end_dt_utc = start_dt_utc + timedelta(seconds=total_duration)

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

        notes_lines = [
            "SNOO Sleep Session Summary:",
            f"\n- Asleep: {fmt_dur(asleep_duration)}",
            f"\n- Soothing: {fmt_dur(soothing_duration)}",
        ]
        
        other_states = defaultdict(float)
        for seg in segments:
            seg_type = seg.get("type", "")
            dur = seg.get("stateDuration")
            if seg_type and isinstance(dur, (int, float)):
                other_states[seg_type] += dur

        for stype, sdur in sorted(other_states.items()):
            if stype.lower() not in ("asleep", "soothing"):
                notes_lines.append(f"- {stype.capitalize()}: {fmt_dur(sdur)}")

        notes = "\n".join(notes_lines)

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
