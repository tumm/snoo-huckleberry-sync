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
from datetime import datetime, timezone, UTC

import aiohttp

from . import config
from .dedupe import DedupeStore
from .huckleberry_sink import make_huckleberry_client, resolve_child_uid, write_sleep_interval
from .session_builder import SleepInterval
from .snoo_source import fetch_device_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    force=True,  # grpc/google-cloud configure root logger before we do; override them
)
log = logging.getLogger("sync.runner")

_MIN_SESSION_SECONDS = 60  # ignore sessions shorter than this (noise / false starts)


async def run_once() -> None:
    dry = config.DRY_RUN
    log.info("Starting sync pass (DRY_RUN=%s, interval=%.0fmin)", dry, config.INTERVAL_MINUTES)

    store = DedupeStore(config.DB_PATH)

    async with aiohttp.ClientSession() as session:
        # ---- Fetch current SNOO device state ----
        state = await fetch_device_state(
            session,
            config.SNOO_USERNAME,
            config.SNOO_PASSWORD,
        )

        # ---- Track new active sessions / update existing ----
        if state.is_active and state.session_id not in ("0", ""):
            now_ms = int(time.time() * 1000)  # wall-clock time of this poll
            if not store.seen(state.session_id):
                existing = {sid: (s, e) for sid, s, e in store.get_active_sessions()}
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
                            "Active session %s has no usable start time (since_start=%d) -skipping",
                            state.session_id,
                            state.since_session_start_ms,
                        )
                else:
                    # Already tracking -advance last-seen to this poll's wall time
                    store.update_active_session_event(state.session_id, now_ms)
                    log.debug("Session %s still active, updated last_event_ms.", state.session_id)

        # ---- Close finished sessions ----
        if not state.is_active:
            active = store.get_active_sessions()
            if not active:
                log.info("No active sessions to close.")
                store.close()
                return

            to_write: list[SleepInterval] = []

            for session_id, start_ms, last_event_ms in active:
                if store.seen(session_id):
                    log.debug("Session %s already written -removing from active.", session_id)
                    store.close_active_session(session_id)
                    continue

                start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
                # Use the last time we observed this session active as the end time.
                # In loop mode this is within one poll interval of the true end.
                end_dt = datetime.fromtimestamp(last_event_ms / 1000, tz=timezone.utc)
                duration_s = (end_dt - start_dt).total_seconds()

                if duration_s < _MIN_SESSION_SECONDS:
                    log.info(
                        "Session %s too short (%.0fs) -discarding.",
                        session_id,
                        duration_s,
                    )
                    store.close_active_session(session_id)
                    continue

                ivl = SleepInterval(
                    session_id=session_id,
                    start=start_dt,
                    end=end_dt,
                    asleep_seconds=duration_s,
                    total_seconds=duration_s,
                )
                to_write.append(ivl)

            if not to_write:
                log.info("No qualifying closed sessions to write.")
                store.close()
                return

            # ---- Dry-run: log and stop ----
            if dry:
                log.info("DRY_RUN=true -logging intended writes, nothing will be written.")
                for ivl in to_write:
                    log.info(
                        "  WOULD WRITE: %s → %s  (%.1f min)",
                        ivl.start.strftime("%Y-%m-%d %H:%M:%S UTC"),
                        ivl.end.strftime("%H:%M:%S UTC"),
                        ivl.total_seconds / 60,
                    )
                log.info("Set DRY_RUN=false in .env when the above intervals look correct.")
                store.close()
                return

            # ---- Real mode: write + mark ----
            hb = await make_huckleberry_client(
                session,
                config.HUCKLEBERRY_EMAIL,
                config.HUCKLEBERRY_PASSWORD,
                config.HUCKLEBERRY_TIMEZONE,
            )
            child_uid = await resolve_child_uid(hb, config.HUCKLEBERRY_CHILD_UID)

            written = 0
            for ivl in to_write:
                await write_sleep_interval(hb, child_uid, ivl)
                store.mark(ivl.session_id, ivl.start, ivl.end)
                store.close_active_session(ivl.session_id)
                written += 1

            await hb.stop_all_listeners()
            log.info("Pass complete: %d session(s) written.", written)

        else:
            log.info("Session %s still active -nothing to write yet.", state.session_id)

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
        if args.loop:
            asyncio.run(run_loop())
        else:
            asyncio.run(run_once())
    except KeyboardInterrupt:
        log.info("Interrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
