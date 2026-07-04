# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                      # install / sync dependencies
uv run python -m sync.runner                 # single poll pass
uv run python -m sync.runner --loop          # continuous polling (Docker entrypoint)
uv run python -m sync.find_child_uids        # print SNOO & Huckleberry child IDs
docker build -t snoo-huckleberry-sync .      # build image
```

There are no tests or linter configs in this project.

## Architecture

**Flow:** `runner` dispatches to one of two modes based on `SNOO_PREMIUM`, gets back completed sessions, checks them against `dedupe` (SQLite), then `huckleberry_sink` writes missing sessions to Huckleberry Firestore using deterministic document IDs based on SNOO `session_id`.

- **Premium mode** (`SNOO_PREMIUM=true`): `snoo_source.fetch_past_sessions` calls `/ss/me/v11/babies/{baby_id}/sessions/daily` to get full session history with a soothing/asleep duration breakdown. **This endpoint requires an active SNOO Premium subscription** — without one it returns `200` with empty `levels: []` regardless of what actually happened on the device, which silently produces zero sessions every pass (confirmed 2026-07-04).
- **Basic/non-premium mode** (`SNOO_PREMIUM=false`, default): `snoo_source.fetch_device_state` polls `/hds/me/v11/devices` for live `activityState.state_machine` and the runner reconstructs completed sessions by tracking `is_active_session` transitions across successive polls (start recorded when a new `session_id` goes active, end recorded from the last poll where it was still active). No soothing/asleep breakdown is available this way — only total duration.

### Module responsibilities

| Module | Role |
|---|---|
| `sync/config.py` | Loads all env vars at import time; raises on missing required vars |
| `sync/ssl_helper.py` | Automatically exports Windows root certificates to PEM for gRPC and provides custom SSL Context |
| `sync/snoo_source.py` | Authenticates with `python-snoo`; `fetch_past_sessions` (premium, history endpoint) and `fetch_device_state` (non-premium, live device polling) |
| `sync/dedupe.py` | SQLite store with two tables: `written_sessions` (permanent, already-synced seen-cache) and `active_sessions` (transient, in-progress session tracking used only in non-premium mode) |
| `sync/huckleberry_sink.py` | Authenticates with `huckleberry-api`, writes sleep intervals with details and location to Firestore and updates `prefs.lastSleep` |
| `sync/runner.py` | Orchestrates one pass or a timed loop; branches into `_run_once_premium` or `_run_once_basic` per `SNOO_PREMIUM` |
| `sync/find_child_uids.py` | Utility script to query and display SNOO and Huckleberry child IDs |

### Key design details

- **Windows Corporate Proxy / SSL Bypass**: On Windows, the runner automatically exports system certificates for gRPC to avoid handshake verification issues. Standard HTTPS queries (`aiohttp`) automatically fall back to `ssl=False` on Windows.
- **Configurable Session Thresholds**: `MIN_SESSION_MINUTES` (configured in `.env`, defaults to 1) specifies the threshold below which SNOO sessions are classified as false starts/noise and discarded.
- **Deterministic ID Enforced Idempotency**: The Huckleberry Firestore document ID (`interval_id`) is generated as a deterministic MD5 hash of the SNOO `session_id`. This guarantees Firestore-level deduplication even if the local database is lost.
- **Sleep Quality Tracking**: SNOO session levels are aggregated to calculate baseline vs. soothing durations, which are automatically formatted and written as a detailed summary to Huckleberry's `notes` field.
- **Sleep Metadata**: Sleep sessions are automatically enriched with locations (`sleepLocations` configured to `onOwnInBed=True`).
- **Local DB Seen-Cache**: `DedupeStore.seen()` guards against redundant Huckleberry Firestore writes to avoid hitting rate limits or Firestore write quotas.
- **Reauth task cancellation**: `python-snoo` schedules a background reauth task; it is cancelled immediately after the devices/sessions fetch because the aiohttp session is torn down after each pass.
- **`DRY_RUN=false` is the default**: sessions are written to Huckleberry. Set `DRY_RUN=true` to log-only mode.

### Data volume

The SQLite file (`DB_PATH`, default `/data/dedupe.sqlite`) must be on a persistent volume in Docker so the dedupe store survives restarts.

### Unofficial APIs & Databases

SNOO uses reverse-engineered API endpoints (via `python-snoo`, calling `/ss/me/v11/babies/{baby_id}/sessions/daily` in premium mode or `/hds/me/v11/devices` in basic mode — neither is part of the `python-snoo` library itself). Huckleberry sleep intervals are written directly to Huckleberry's Google Firebase Firestore database using the official Google Cloud Firestore SDK. Changes in either platform's backend structure can silently break this tool.
