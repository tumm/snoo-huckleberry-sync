# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                      # install / sync dependencies
uv run python -m sync.runner                 # single poll pass
uv run python -m sync.runner --loop          # continuous polling (Docker entrypoint)
docker build -t snoo-huckleberry-sync .      # build image
```

There are no tests or linter configs in this project.

## Architecture

**Flow:** `snoo_source` polls the SNOO REST API → `runner` tracks session lifecycle in `dedupe` (SQLite) → when a session closes, `huckleberry_sink` writes it to Firestore.

### Module responsibilities

| Module | Role |
|---|---|
| `sync/config.py` | Loads all env vars at import time; raises on missing required vars |
| `sync/snoo_source.py` | Authenticates with `python-snoo`, calls `/hds/me/v11/devices`, returns `SnooDeviceState` |
| `sync/session_builder.py` | `SleepInterval` dataclass — shared between runner and sink |
| `sync/dedupe.py` | SQLite store with two tables: `active_sessions` (transient, in-progress) and `written_sessions` (permanent, already synced) |
| `sync/huckleberry_sink.py` | Authenticates with `huckleberry-api`, writes sleep intervals to Firestore and updates `prefs.lastSleep` |
| `sync/runner.py` | Orchestrates one pass or a timed loop; handles the session state machine |

### Key design details

- **End-time approximation**: the session end time is the wall-clock time of the last poll that saw the session active. Accuracy is within one `INTERVAL_MINUTES` window.
- **Session start back-computation**: SNOO reports `since_session_start_ms` (elapsed) and `event_time_ms` (timestamp); start = `event_time_ms - since_session_start_ms`.
- **`is_active_session` is a string**: the SNOO API returns `"true"`/`"false"` (not a JSON bool); `snoo_source.py` handles the coercion.
- **Reauth task cancellation**: `python-snoo` schedules a background reauth task; it is cancelled immediately after the device fetch because the aiohttp session is torn down after each pass.
- **`DRY_RUN=true` is the default**: nothing is written to Huckleberry until explicitly set to `false`.
- **Idempotency**: `DedupeStore.seen()` guards against double-writes; `DedupeStore.mark()` is called atomically after a successful Firestore write.

### Data volume

The SQLite file (`DB_PATH`, default `/data/dedupe.sqlite`) must be on a persistent volume in Docker so the dedupe store survives restarts.

### Unofficial APIs

Both SNOO and Huckleberry use reverse-engineered APIs. Changes in either app's backend can silently break this tool. The SNOO endpoint is `https://api-us-east-1-prod.happiestbaby.com/hds/me/v11/devices`; Huckleberry writes go through Firebase Firestore via the `huckleberry-api` library.
