"""
Poll SNOO device state → track sessions → write to Huckleberry (or dry-run log).

Run once:
    python -m sync.runner

Or in a loop (Docker entrypoint):
    python -m sync.runner --loop
"""

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Callable


import aiohttp
from python_snoo.containers import SnooData

# Run Windows gRPC SSL setup before importing packages that use gRPC
from .ssl_helper import get_ssl_context, setup_grpc_ssl
setup_grpc_ssl()

from . import config
from .dedupe import DedupeStore
from .huckleberry_sink import make_huckleberry_client, resolve_child_uid, write_sleep_interval
from .live_source import LiveSessionTracker
from .snoo_source import fetch_past_sessions, fetch_device_state, start_live_subscription, SnooCompletedSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    force=True,  # grpc/google-cloud configure root logger before we do; override them
)
log = logging.getLogger("sync.runner")

_MIN_SESSION_SECONDS = config.MIN_SESSION_MINUTES * 60  # ignore sessions shorter than this (noise / false starts)

_NO_BREAKDOWN_NOTES = (
    "SNOO Sleep Session (basic tracking - total duration only).\n"
    "Set SNOO_MODE=premium or SNOO_MODE=live in .env for an asleep/soothing breakdown."
)


async def _write_batch(
    session: aiohttp.ClientSession,
    store: DedupeStore,
    to_write: list[SnooCompletedSession],
    dry: bool,
    on_write: Callable[[SnooCompletedSession], None] | None = None,
) -> None:
    """Write completed sessions to Huckleberry (or log them in dry-run mode).

    `on_write` fires only for sessions that were actually written (never in dry-run),
    so callers can safely use it to release any in-progress tracking state - e.g.
    non-premium mode's active_sessions rows must survive a dry-run pass so the
    tracked start time isn't lost before a real write ever happens.
    """
    if not to_write:
        log.info("No new completed sleep sessions to write.")
        return

    if dry:
        log.info("DRY_RUN=true; logging intended writes only, nothing will be written.")
        for sess in to_write:
            log.info(
                "  WOULD WRITE: %s -> %s  (%.1f min)\nNotes:\n%s",
                sess.start.strftime("%Y-%m-%d %H:%M:%S UTC"),
                sess.end.strftime("%H:%M:%S UTC"),
                sess.total_seconds / 60,
                sess.notes,
            )
        log.info("Set DRY_RUN=false in .env when the above intervals look correct.")
        return

    hb = await make_huckleberry_client(
        session,
        config.HUCKLEBERRY_EMAIL,
        config.HUCKLEBERRY_PASSWORD,
        config.HUCKLEBERRY_TIMEZONE,
    )
    child_uid = await resolve_child_uid(hb, config.HUCKLEBERRY_CHILD_UID)

    written = 0
    for sess in to_write:
        await write_sleep_interval(hb, child_uid, sess)
        store.mark(sess.session_id, sess.start, sess.end)
        if on_write:
            on_write(sess)
        written += 1

    await hb.stop_all_listeners()
    log.info("Pass complete: %d session(s) written.", written)


async def _run_once_premium(session: aiohttp.ClientSession, store: DedupeStore, dry: bool) -> None:
    """SNOO Premium mode: pull full session history (with soothing/asleep breakdown)."""
    try:
        past_sessions = await fetch_past_sessions(
            session,
            config.SNOO_USERNAME,
            config.SNOO_PASSWORD,
            config.HUCKLEBERRY_TIMEZONE,
            days=config.HISTORY_DAYS,
            baby_id_override=config.SNOO_BABY_ID,
        )
    except Exception as exc:
        log.error("Failed to fetch SNOO history: %s", exc, exc_info=True)
        return

    to_write: list[SnooCompletedSession] = []
    for sess in past_sessions:
        if store.seen(sess.session_id):
            log.debug("Session %s already written, skipping.", sess.session_id)
            continue

        if sess.total_seconds < _MIN_SESSION_SECONDS:
            log.info(
                "Session %s too short (%.0fs) - discarding.",
                sess.session_id,
                sess.total_seconds,
            )
            continue

        # Skip sessions that are in progress or ended too recently (using the configured buffer)
        # This ensures we only sync completed sleep sessions and don't cache premature durations.
        now_utc = datetime.now(timezone.utc)
        if now_utc - sess.end < timedelta(minutes=config.IN_PROGRESS_BUFFER_MINUTES):
            log.info(
                "Session %s is in progress or ended too recently (ended %s) - skipping for now.",
                sess.session_id,
                sess.end.strftime("%Y-%m-%d %H:%M:%S UTC"),
            )
            continue

        to_write.append(sess)

    await _write_batch(session, store, to_write, dry)


async def _run_once_basic(session: aiohttp.ClientSession, store: DedupeStore, dry: bool) -> None:
    """Non-premium mode: no history endpoint access, so reconstruct sessions by polling
    live device state and tracking is_active_session transitions across polls."""
    try:
        state = await fetch_device_state(session, config.SNOO_USERNAME, config.SNOO_PASSWORD)
    except Exception as exc:
        log.error("Failed to fetch SNOO device state: %s", exc, exc_info=True)
        return

    # ---- Track new/ongoing active session ----
    if state.is_active and state.session_id not in ("0", ""):
        now_ms = int(time.time() * 1000)  # wall-clock time of this poll
        if not store.seen(state.session_id):
            existing = {sid for sid, _, _ in store.get_active_sessions()}
            if state.session_id not in existing:
                start_ms = state.session_start_ms
                if start_ms is not None:
                    store.record_active_session(state.session_id, start_ms, now_ms)
                    log.info(
                        "Tracking new SNOO session %s (started %s)",
                        state.session_id,
                        state.session_start.isoformat() if state.session_start else "unknown",
                    )
                else:
                    log.warning(
                        "Active session %s has no usable start time (since_start=%d), skipping",
                        state.session_id,
                        state.since_session_start_ms,
                    )
            else:
                store.update_active_session_event(state.session_id, now_ms)
                log.debug("Session %s still active, updated last_event_ms.", state.session_id)
        log.info("Session %s still active - nothing to write yet.", state.session_id)
        return

    # ---- Device inactive: close out any sessions we were tracking ----
    active = store.get_active_sessions()
    if not active:
        log.info("No active sessions to close.")
        return

    to_write: list[SnooCompletedSession] = []
    for session_id, start_ms, last_event_ms in active:
        if store.seen(session_id):
            log.debug("Session %s already written, removing from active.", session_id)
            store.close_active_session(session_id)
            continue

        start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        # Use the last time we observed this session active as the end time.
        # In loop mode this is within one poll interval of the true end.
        end_dt = datetime.fromtimestamp(last_event_ms / 1000, tz=timezone.utc)
        duration_s = (end_dt - start_dt).total_seconds()

        if duration_s < _MIN_SESSION_SECONDS:
            log.info("Session %s too short (%.0fs) - discarding.", session_id, duration_s)
            store.close_active_session(session_id)
            continue

        to_write.append(SnooCompletedSession(
            session_id=session_id,
            start=start_dt,
            end=end_dt,
            total_seconds=duration_s,
            notes=_NO_BREAKDOWN_NOTES,
        ))

    await _write_batch(
        session, store, to_write, dry,
        on_write=lambda sess: store.close_active_session(sess.session_id),
    )


async def _write_one_live_session(
    store: DedupeStore,
    hb,
    child_uid: str | None,
    sess: SnooCompletedSession,
    dry: bool,
) -> None:
    if dry:
        log.info(
            "  WOULD WRITE: %s -> %s  (%.1f min)\nNotes:\n%s",
            sess.start.strftime("%Y-%m-%d %H:%M:%S UTC"),
            sess.end.strftime("%H:%M:%S UTC"),
            sess.total_seconds / 60,
            sess.notes,
        )
        return

    if store.seen(sess.session_id):
        log.debug("Live session %s already written, skipping.", sess.session_id)
        return

    # This runs as a fire-and-forget asyncio.create_task from on_message, with no
    # caller left to see an exception - by the time this session reaches here its
    # source events are already cleared from SQLite (LiveSessionTracker's job is
    # done), so a failure here means the reconstructed session data is gone for
    # good unless it's at least logged loudly for a human to notice and recover
    # manually (e.g. from these log lines) rather than vanishing into asyncio's
    # generic "Task exception was never retrieved" warning.
    try:
        await write_sleep_interval(hb, child_uid, sess)
        store.mark(sess.session_id, sess.start, sess.end)
        log.info("Live session %s written to Huckleberry.", sess.session_id)
    except Exception:
        log.error(
            "Failed to write live session %s to Huckleberry - this session's data is "
            "now lost (source events already cleared). start=%s end=%s notes:\n%s",
            sess.session_id,
            sess.start.isoformat(),
            sess.end.isoformat(),
            sess.notes,
            exc_info=True,
        )


async def _run_live() -> None:
    """Persistent live mode: never returns. Listens to AWS IoT MQTT push
    events and writes completed sessions to Huckleberry as they close,
    instead of waiting for a poll interval."""
    dry = config.DRY_RUN
    log.info("Starting live mode (DRY_RUN=%s) - persistent MQTT session tracking.", dry)

    store = DedupeStore(config.DB_PATH)
    tracker = LiveSessionTracker(store, _MIN_SESSION_SECONDS)

    import os
    connector = aiohttp.TCPConnector(ssl=False) if os.name == "nt" else None
    async with aiohttp.ClientSession(connector=connector) as session:
        hb = None
        child_uid = None
        if not dry:
            hb = await make_huckleberry_client(
                session, config.HUCKLEBERRY_EMAIL, config.HUCKLEBERRY_PASSWORD, config.HUCKLEBERRY_TIMEZONE,
            )
            child_uid = await resolve_child_uid(hb, config.HUCKLEBERRY_CHILD_UID)

        # Strong references to fire-and-forget write tasks - asyncio.Task only
        # holds a weak reference internally, so without this a task can be
        # garbage-collected mid-flight, silently dropping a write with no
        # error logged at all.
        _pending_writes: set[asyncio.Task] = set()

        def on_message(data: SnooData) -> None:
            try:
                completed = tracker.handle_event(data)
            except Exception:
                log.error("Error handling live event, dropping it.", exc_info=True)
                return
            for sess in completed:
                task = asyncio.create_task(_write_one_live_session(store, hb, child_uid, sess, dry))
                _pending_writes.add(task)
                task.add_done_callback(_pending_writes.discard)

        snoo, device = await start_live_subscription(
            session, config.SNOO_USERNAME, config.SNOO_PASSWORD, on_message
        )

        # Heartbeat + resubscribe watchdog. python-snoo doesn't expose a public
        # "is this subscription alive" API, so this reaches into its internal
        # _mqtt_tasks map - fragile if the library restructures, but there's no
        # supported alternative and the cost of missing a dead connection in an
        # unattended Portainer deployment is silent, indefinite data loss.
        heartbeat_s = config.INTERVAL_MINUTES * 60
        while True:
            await asyncio.sleep(heartbeat_s)
            task = snoo._mqtt_tasks.get(device.serialNumber)
            if task is None or task.done():
                log.warning("Live MQTT subscription for %s is not running - resubscribing.", device.serialNumber)
                snoo.start_subscribe(device, on_message)
                # Re-seed a fresh read of the device's actual current session_id as
                # soon as possible after reconnecting, rather than waiting for the
                # next organic transition - otherwise a session that was open before
                # the connection dropped never gets closed until some unrelated
                # future session's closing event arrives (see
                # LiveSessionTracker._close_open_sessions's stale-session handling).
                try:
                    await snoo.get_status(device)
                except Exception:
                    log.warning(
                        "Post-resubscribe device status request for %s failed or "
                        "timed out; will pick up state on the next real transition.",
                        device.serialNumber,
                        exc_info=True,
                    )
            else:
                log.info("Live mode heartbeat: MQTT subscription alive for %s.", device.serialNumber)


async def run_once() -> None:
    dry = config.DRY_RUN
    log.info("Starting sync pass (DRY_RUN=%s, mode=%s, interval=%.0fmin)", dry, config.SNOO_MODE, config.INTERVAL_MINUTES)

    store = DedupeStore(config.DB_PATH)

    import os
    connector = aiohttp.TCPConnector(ssl=False) if os.name == "nt" else None
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            if config.SNOO_MODE == "premium":
                await _run_once_premium(session, store, dry)
            else:
                await _run_once_basic(session, store, dry)
    finally:
        store.close()


async def run_loop() -> None:
    interval_s = config.INTERVAL_MINUTES * 60
    log.info("Starting sync loop (interval=%.0f min).", config.INTERVAL_MINUTES)
    while True:
        try:
            await run_once()
        except Exception as exc:
            log.error("Sync pass failed: %s", exc, exc_info=True)
        log.info("Sleeping %.0f seconds until next pass.", interval_s)
        await asyncio.sleep(interval_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="SNOO → Huckleberry sync")
    parser.add_argument("--loop", action="store_true", help="Run continuously on INTERVAL_MINUTES schedule")
    args = parser.parse_args()

    try:
        if config.SNOO_MODE == "live":
            asyncio.run(_run_live())
        elif args.loop:
            asyncio.run(run_loop())
        else:
            asyncio.run(run_once())
    except KeyboardInterrupt:
        log.info("Interrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
