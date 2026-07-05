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

**Flow:** `runner` dispatches to one of three modes based on `SNOO_MODE`, gets back completed sessions, checks them against `dedupe` (SQLite), then `huckleberry_sink` writes missing sessions to Huckleberry Firestore using deterministic document IDs based on SNOO `session_id`.

- **Live mode** (`SNOO_MODE=live`, default): `snoo_source.start_live_subscription` opens a persistent AWS IoT MQTT push subscription (the same mechanism the official Home Assistant SNOO integration uses - confirmed by reading its source). `runner._run_live()` never returns; `live_source.LiveSessionTracker` reconstructs completed sessions from real-time state transitions (persisted to SQLite as they arrive, so a restart mid-session resumes rather than loses data), giving a minute-level asleep/soothing/other-state breakdown plus a best-effort wake-reason guess - all without needing SNOO Premium. `--loop` is ignored in this mode since the connection itself never returns. Unlike `run_loop`'s per-pass exception handling, `_run_live()` has no outer retry loop - a crash (e.g. an auth failure at startup) exits the process, so live mode deployments rely on the container's restart policy for recovery.
- **Basic mode** (`SNOO_MODE=basic`): polls `/hds/me/v11/devices` every `INTERVAL_MINUTES` and reconstructs sessions from `is_active_session` transitions across polls (kept as a fallback). No breakdown, only total duration.
- **Premium mode** (`SNOO_MODE=premium`): `snoo_source.fetch_past_sessions` calls `/ss/me/v11/babies/{baby_id}/sessions/daily` for full session history with a soothing/asleep breakdown. **Requires an active SNOO Premium subscription** - without one it returns `200` with empty `levels: []` regardless of real activity (confirmed 2026-07-04).

### Module responsibilities

| Module | Role |
|---|---|
| `sync/config.py` | Loads all env vars at import time; raises on missing required vars |
| `sync/ssl_helper.py` | Automatically exports Windows root certificates to PEM for gRPC and provides custom SSL Context |
| `sync/snoo_source.py` | Authenticates with `python-snoo`; `fetch_past_sessions` (premium), `fetch_device_state` (basic polling), `start_live_subscription` (live MQTT push); shared `aggregate_segment_durations`/`format_session_notes` helpers |
| `sync/dedupe.py` | SQLite store with three tables: `written_sessions` (permanent seen-cache), `active_sessions` (transient, basic-mode in-progress tracking), `live_session_events` (transient, live-mode per-transition event log) |
| `sync/live_source.py` | `LiveSessionTracker` - pure session-reconstruction logic from live MQTT events, no network/Firestore I/O |
| `sync/huckleberry_sink.py` | Authenticates with `huckleberry-api`, writes sleep intervals with details and location to Firestore and updates `prefs.lastSleep` |
| `sync/runner.py` | Orchestrates one pass or a timed loop; branches into `_run_live`, `_run_once_basic`, or `_run_once_premium` per `SNOO_MODE` |
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

SNOO uses reverse-engineered API endpoints (via `python-snoo`, calling `/ss/me/v11/babies/{baby_id}/sessions/daily` in premium mode, `/hds/me/v11/devices` in basic mode, or AWS IoT MQTT in live mode — the HTTP endpoints are not part of the `python-snoo` library itself). Huckleberry sleep intervals are written directly to Huckleberry's Google Firebase Firestore database using the official Google Cloud Firestore SDK. Changes in either platform's backend structure can silently break this tool.
