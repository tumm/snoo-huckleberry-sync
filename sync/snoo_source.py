"""SNOO data source: poll /hds/me/v11/devices for current activity state."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
from python_snoo.snoo import Snoo

log = logging.getLogger(__name__)

DEVICES_URL = "https://api-us-east-1-prod.happiestbaby.com/hds/me/v11/devices"


@dataclass
class SnooDeviceState:
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
        return datetime.utcfromtimestamp(ms / 1000).replace(tzinfo=timezone.utc)

    @property
    def event_time(self) -> datetime:
        return datetime.utcfromtimestamp(self.event_time_ms / 1000).replace(tzinfo=timezone.utc)


async def fetch_device_state(
    websession: aiohttp.ClientSession,
    username: str,
    password: str,
) -> SnooDeviceState:
    """Authenticate and return the current SNOO activity state."""
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

    # Cancel the background reauth task -our aiohttp session is short-lived
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
