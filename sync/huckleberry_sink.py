"""Write completed sleep intervals to Huckleberry via Firebase Firestore."""

import hashlib
import logging
import time

import aiohttp
from huckleberry_api import HuckleberryAPI
from huckleberry_api.firebase_types import (
    FirebaseLastSleepData,
    FirebaseSleepDetails,
    FirebaseSleepIntervalData,
    FirebaseSleepLocations,
    to_firebase_dict,
)

from .snoo_source import SnooCompletedSession
from . import config

log = logging.getLogger(__name__)


async def resolve_child_uid(hb: HuckleberryAPI, override: str | None) -> str:
    """Return the Huckleberry child UID from env override or first child in account."""
    if override:
        return override
    user = await hb.get_user()
    if not user or not user.childList:
        raise RuntimeError(
            "Could not find any children in Huckleberry account. "
            "Set HUCKLEBERRY_CHILD_UID in .env to bypass auto-detection."
        )
    cid = user.childList[0].cid
    log.info("Auto-detected child UID: %s", cid)
    return cid


async def write_sleep_interval(
    hb: HuckleberryAPI,
    child_uid: str,
    interval: SnooCompletedSession,
) -> None:
    """Write one sleep interval to Firestore sleep/{child_uid}/intervals."""
    start_sec = interval.start.timestamp()
    duration_sec = int((interval.end - interval.start).total_seconds())
    tz_offset = await hb._get_timezone_offset_minutes()

    client = await hb._get_firestore_client()
    sleep_ref = client.collection("sleep").document(child_uid)

    interval_id = hashlib.sha256(interval.session_id.encode("utf-8")).hexdigest()[:16]
    
    loc_key = config.HUCKLEBERRY_SLEEP_LOCATION
    valid_locations = {
        "car", "nursing", "wornOrHeld", "stroller", "coSleep", 
        "nextToCarer", "onOwnInBed", "bottle", "swing"
    }
    if loc_key not in valid_locations:
        log.warning(
            "Invalid HUCKLEBERRY_SLEEP_LOCATION '%s', falling back to 'onOwnInBed'. Valid choices: %s", 
            loc_key, sorted(valid_locations)
        )
        loc_key = "onOwnInBed"

    loc_kwargs = {loc_key: True}
    details = FirebaseSleepDetails(
        notes=interval.notes,
        sleepLocations=FirebaseSleepLocations(**loc_kwargs),
    )
    
    sleep_data = FirebaseSleepIntervalData(
        start=start_sec,
        duration=duration_sec,
        offset=tz_offset,
        end_offset=tz_offset,
        details=details,
        lastUpdated=time.time(),
    )
    
    payload = to_firebase_dict(sleep_data)

    await sleep_ref.collection("intervals").document(interval_id).set(payload)
    log.info(
        "Wrote sleep interval %s to Huckleberry (doc %s): start=%s duration=%ds",
        interval.session_id,
        interval_id,
        interval.start.isoformat(),
        duration_sec,
    )

    # Update prefs.lastSleep only when this interval is newer than the stored one.
    # Best-effort: a failure here must not prevent the dedupe mark from running.
    try:
        now = time.time()
        sleep_doc = await sleep_ref.get()
        existing_last_start = 0.0
        if sleep_doc.exists:
            prefs = (sleep_doc.to_dict() or {}).get("prefs", {})
            existing_last_start = float((prefs.get("lastSleep") or {}).get("start") or 0)

        if start_sec > existing_last_start:
            last_sleep = FirebaseLastSleepData(
                start=start_sec,
                duration=duration_sec,
                offset=tz_offset,
            )
            # set(..., merge=True) creates the parent document if it doesn't exist yet,
            # unlike update() which requires it to already exist.
            await sleep_ref.set(
                {
                    "prefs": {
                        "lastSleep": to_firebase_dict(last_sleep),
                        "timestamp": {"seconds": now},
                        "local_timestamp": now,
                    }
                },
                merge=True,
            )
            log.debug("Updated prefs.lastSleep for child %s", child_uid)
    except Exception:
        log.warning("Failed to update prefs.lastSleep; interval already written, continuing.", exc_info=True)


async def make_huckleberry_client(
    websession: aiohttp.ClientSession,
    email: str,
    password: str,
    timezone: str,
) -> HuckleberryAPI:
    hb = HuckleberryAPI(email, password, timezone, websession)
    await hb.authenticate()
    return hb

