"""Write completed sleep intervals to Huckleberry via Firebase Firestore."""

import hashlib
import logging
import time

import aiohttp
from google.cloud.firestore_v1 import async_transactional
from google.cloud.firestore_v1.async_transaction import AsyncTransaction
from huckleberry_api import HuckleberryAPI
from huckleberry_api.firebase_types import (
    FirebaseLastSleepData,
    FirebaseSleepDetails,
    FirebaseSleepIntervalData,
    FirebaseSleepLocations,
    to_firebase_dict,
)

from . import config
from .models import SnooCompletedSession

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
    """Write one sleep interval to Firestore sleep/{child_uid}/intervals and update
    prefs.lastSleep, both inside a single transaction.

    Both writes are transactional so a crash/exception between them can't leave the
    interval durably written with prefs.lastSleep never updated (or vice versa) - the
    whole write either fully commits or fully fails. A failure here propagates to the
    caller (unlike a prior best-effort try/except around only the lastSleep update),
    so runner.py's retry logic and store.mark()-gating correctly treat a failed write
    as "not yet written" rather than a false success.

    interval_id is a deterministic hash of the SNOO session_id, so re-running this
    transaction (e.g. Firestore's automatic contention retry, or a future pass
    re-attempting a session whose prior write failed) overwrites it with identical
    data - it stays idempotent.
    """
    start_sec = interval.start.timestamp()
    duration_sec = int((interval.end - interval.start).total_seconds())
    tz_offset = await hb._get_timezone_offset_minutes()

    client = await hb._get_firestore_client()
    sleep_ref = client.collection("sleep").document(child_uid)

    interval_id = hashlib.sha256(interval.session_id.encode("utf-8")).hexdigest()[:16]
    interval_ref = sleep_ref.collection("intervals").document(interval_id)

    loc_key = config.HUCKLEBERRY_SLEEP_LOCATION
    if loc_key not in config.VALID_SLEEP_LOCATIONS:
        log.warning(
            "Invalid HUCKLEBERRY_SLEEP_LOCATION '%s', falling back to 'onOwnInBed'. Valid choices: %s",
            loc_key, sorted(config.VALID_SLEEP_LOCATIONS),
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
    interval_payload = to_firebase_dict(sleep_data)
    now = time.time()

    @async_transactional
    async def _write_txn(txn: AsyncTransaction) -> bool:
        # Firestore transactions require every read to precede every write in the
        # same transaction - this read must stay first, before either txn.set() call.
        sleep_doc = await sleep_ref.get(transaction=txn)
        existing_last_start = 0.0
        if sleep_doc.exists:
            prefs = (sleep_doc.to_dict() or {}).get("prefs", {})
            existing_last_start = float((prefs.get("lastSleep") or {}).get("start") or 0)

        # AsyncDocumentReference.set() has no `transaction` kwarg - writes inside a
        # transaction go through the transaction object's own .set().
        txn.set(interval_ref, interval_payload)

        updated_last_sleep = False
        if start_sec > existing_last_start:
            last_sleep = FirebaseLastSleepData(
                start=start_sec,
                duration=duration_sec,
                offset=tz_offset,
            )
            # merge=True creates the parent document if it doesn't exist yet,
            # unlike a plain overwrite which requires it to already exist.
            txn.set(
                sleep_ref,
                {
                    "prefs": {
                        "lastSleep": to_firebase_dict(last_sleep),
                        "timestamp": {"seconds": now},
                        "local_timestamp": now,
                    }
                },
                merge=True,
            )
            updated_last_sleep = True
        return updated_last_sleep

    updated = await _write_txn(client.transaction())
    log.info(
        "Wrote sleep interval %s to Huckleberry (doc %s): start=%s duration=%ds",
        interval.session_id,
        interval_id,
        interval.start.isoformat(),
        duration_sec,
    )
    if updated:
        log.debug("Updated prefs.lastSleep for child %s", child_uid)


async def make_huckleberry_client(
    websession: aiohttp.ClientSession,
    email: str,
    password: str,
    timezone: str,
) -> HuckleberryAPI:
    hb = HuckleberryAPI(email, password, timezone, websession)
    await hb.authenticate()
    return hb

