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


import aiohttp

# Run Windows gRPC SSL setup before importing packages that use gRPC
from .ssl_helper import get_ssl_context, setup_grpc_ssl
setup_grpc_ssl()

from . import config
from .dedupe import DedupeStore
from .huckleberry_sink import make_huckleberry_client, resolve_child_uid, write_sleep_interval
from .snoo_source import fetch_past_sessions, SnooCompletedSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    force=True,  # grpc/google-cloud configure root logger before we do; override them
)
log = logging.getLogger("sync.runner")

_MIN_SESSION_SECONDS = config.MIN_SESSION_MINUTES * 60  # ignore sessions shorter than this (noise / false starts)


async def run_once() -> None:
    dry = config.DRY_RUN
    log.info("Starting sync pass (DRY_RUN=%s, interval=%.0fmin)", dry, config.INTERVAL_MINUTES)

    store = DedupeStore(config.DB_PATH)

    import os
    connector = aiohttp.TCPConnector(ssl=False) if os.name == "nt" else None
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            # ---- Fetch SNOO history ----
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
                # Skip already written sessions
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

                to_write.append(sess)

            if not to_write:
                log.info("No new completed sleep sessions to write.")
                return

            # ---- Dry-run: log and stop ----
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

            # ---- Real mode: write + mark ----
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
                written += 1

            await hb.stop_all_listeners()
            log.info("Pass complete: %d session(s) written.", written)
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
        if args.loop:
            asyncio.run(run_loop())
        else:
            asyncio.run(run_once())
    except KeyboardInterrupt:
        log.info("Interrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
