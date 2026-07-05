# Live MQTT session tracking — design

## Goal

Add a third session-tracking mode, `live`, that reconstructs completed SNOO
sleep sessions from the same real-time push events the official Home
Assistant SNOO integration uses — giving minute-level asleep/soothing
breakdown without requiring a SNOO Premium subscription. Make it the new
default, since it is strictly better than the current `basic` polling mode
for accounts without Premium.

## Background

- `SNOO_PREMIUM=true` (existing `premium` mode): fetches
  `/ss/me/v11/babies/{id}/sessions/daily`. Requires an active SNOO Premium
  subscription; without one it returns `200` with empty data regardless of
  real activity (confirmed live against this account; fixed in commit
  `9e63c9a`, see `sync/runner.py::_run_once_premium`).
- `SNOO_PREMIUM=false` (existing `basic` mode, current default): polls
  `/hds/me/v11/devices` every `INTERVAL_MINUTES` and reconstructs sessions
  from `is_active_session` transitions across polls. Works without Premium,
  but only yields total duration — no soothing/asleep breakdown — and has
  up to one poll interval of imprecision on start/end times.
- Investigated the real Home Assistant SNOO integration source
  (`home-assistant/core`, `homeassistant/components/snoo/coordinator.py`):
  it calls `snoo.start_subscribe(device, callback)`, which is `python-snoo`'s
  **AWS IoT MQTT** push subscription (`iot_class: cloud_push` in its
  manifest) — not PubNub as originally assumed. `python-snoo` 0.11.0 (already
  installed here) implements this in `snoo.py`'s `start_subscribe` /
  `subscribe_mqtt`, connecting over websockets to the device's
  `awsIoT.clientEndpoint` and subscribing to
  `{thingName}/state_machine/activity_state`. It delivers a `SnooData`
  object on every state transition, each carrying `event_time_ms` and a
  `state_machine` (`session_id`, `is_active_session`, `since_session_start_ms`,
  `state` — one of `BASELINE`/`WEANING_BASELINE`/`LEVEL1`-`LEVEL4`/`ONLINE`
  (stop)/`PRETIMEOUT`/`TIMEOUT`/`SUSPENDED`/etc.) and an `event` enum
  (`ACTIVITY`, `CRY`, `SAFETY_CLIP`, `TIMER`, `POWER`, ...). Token refresh and
  MQTT resubscription on reauth are already handled internally by
  `python-snoo` (`schedule_reauthorization`).
- This means real event timestamps are pushed exactly at each state
  transition — precise segment durations come for free, the same shape of
  data `premium` mode's `levels` list provides, just derived from live
  pushes instead of a history API call.

## Scope

In scope:
- New `SNOO_MODE` config replacing `SNOO_PREMIUM` boolean: `premium` |
  `basic` | `live`, default `live`.
- New `live` mode: persistent MQTT listener, session reconstruction with
  asleep/soothing/other-state breakdown, wake-reason heuristic, writes to
  Huckleberry as sessions complete (not batched on a poll cadence).
- Shared extraction of the segment-duration-aggregation/notes-formatting
  logic currently embedded in `snoo_source.fetch_past_sessions`, so `premium`
  and `live` modes both call the same helper.
- Crash/restart resilience for in-progress sessions via a new SQLite table.
- Docker/runner dispatch changes needed to run `live` mode as a persistent
  process instead of a poll loop.

Out of scope:
- Changing `premium` or `basic` mode's fetch logic — untouched.
- A watchdog/alerting system beyond a basic resubscribe-if-dead check and
  heartbeat logging.
- Automated tests (project has no test framework; verified via synthetic
  event scripts and a dry-run smoke test against the real account instead).
- Precisely validating the wake-reason heuristic against a real session —
  no real session data available at design time; first real runs in
  `DRY_RUN=true` will confirm or require adjustment.

## Config changes

`sync/config.py`:
- Replace `SNOO_PREMIUM: bool` with `SNOO_MODE: str`, one of
  `"premium" | "basic" | "live"`, default `"live"`. Raise a clear
  `RuntimeError` if set to anything else.
- `HISTORY_DAYS` / `IN_PROGRESS_BUFFER_MINUTES` remain, documented as
  premium-only (unchanged).
- No new required env vars for `live` mode — it reuses `SNOO_USERNAME`/
  `SNOO_PASSWORD`/`HUCKLEBERRY_*`/`MIN_SESSION_MINUTES`/`DB_PATH`.

`.env.example` updated to document the three-way `SNOO_MODE` and note that
`live` is the recommended default for non-Premium accounts.

## Data flow (live mode)

```
Snoo.authorize() → get_devices() → get_status(device) [seed initial state]
  → start_subscribe(device, on_message)   # persistent MQTT task inside python-snoo
        │
        ▼ (every state transition, pushed in real time)
  on_message(SnooData)
        │
        ├─ is_active_session=True:
        │     • new session_id → open it (compute start_ms from
        │       event_time_ms - since_session_start_ms, same back-compute
        │       already used in basic mode's SnooDeviceState)
        │     • append (session_id, event_time_ms, state) row to
        │       live_session_events (SQLite)
        │
        └─ is_active_session=False, session was open:
              • read back all live_session_events rows for that session_id
              • compute per-state durations from consecutive event_time_ms
                deltas (last segment ends at this closing event's time)
              • bucket durations: BASELINE/WEANING_BASELINE → Asleep,
                LEVEL1-4 → Soothing (+ per-level detail), others listed
                individually (mirrors premium's "other states" notes format)
              • derive wake-reason heuristic from the last few events
              • discard if total duration < MIN_SESSION_MINUTES
              • build SnooCompletedSession, write immediately via
                write_sleep_interval (or log if DRY_RUN), store.mark(),
                delete the live_session_events rows for that session_id
```

Crash recovery: because every event is persisted to SQLite as it arrives
(not held only in memory), a process restart mid-session just resumes
appending to the same session's existing rows once the first post-restart
event for that `session_id` arrives — no data loss beyond whatever gap the
restart itself caused.

## New/changed modules

- `sync/dedupe.py`: add `live_session_events` table (`session_id`,
  `event_time_ms`, `state`, ordered by insertion) with
  `append_live_event`, `get_live_events(session_id)`,
  `clear_live_events(session_id)`, `open_live_session_ids()` (distinct
  session_ids currently tracked, for resuming after restart / knowing
  what's in-progress).
- `sync/snoo_source.py`: extract `_aggregate_segments(segments) -> (asleep_s,
  soothing_s, other: dict[str, float])` and `_format_notes(...)` out of
  `fetch_past_sessions` into shared functions; add `resolve_device()` helper
  (wraps `get_devices()` to return the `SnooDevice` object, needed by
  `start_subscribe`, not just the baby_id `fetch_past_sessions` uses).
- `sync/live_source.py` (new): `LiveSessionTracker` — owns the SQLite-backed
  open/append/close logic and the wake-reason heuristic; exposes
  `handle_event(data: SnooData) -> SnooCompletedSession | None` (returns a
  completed session when one just closed, else `None`). Pure logic, no I/O
  to Huckleberry — keeps it testable with synthetic events and independent
  of the MQTT plumbing.
- `sync/runner.py`: add `_run_live()` — auth, resolve device, create
  Huckleberry client once, `start_subscribe` with a callback that calls
  `LiveSessionTracker.handle_event` and writes/logs any returned completed
  session immediately; heartbeat log + dead-task resubscribe check on a
  timer; blocks forever. `main()` dispatches to `asyncio.run(_run_live())`
  when `SNOO_MODE == "live"`, bypassing `--loop`/`run_once` entirely.

## Wake-reason heuristic (best-effort, from the closing events of a session)

Checked in this order against the last few events before `is_active_session`
flips to `False`:
1. safety clip count drops from 1 to 0 → "Picked up out of SNOO"
2. final `state` is `TIMEOUT` → "Soothing timed out without settling"
3. final `state` is `SUSPENDED` → "Session stopped manually"
4. a `CRY` event immediately precedes the end → "Ended after sustained crying"
5. none match → omit the line (don't guess)

Flagged as unvalidated against real data; expect to revisit after the first
few real sessions land in the logs.

## Notes format (live mode)

Mirrors premium's existing style via the shared formatting helper, with the
wake-reason appended:
```
SNOO Sleep Session Summary:
- Asleep: 2h 10m
- Soothing: 25m
- Level2: 10m
- Ended: Picked up out of SNOO
```

## Error handling & resilience

- `on_message` callback wrapped in try/except — a malformed/unexpected event
  is logged and dropped, never kills the listener.
- `python-snoo` already retries MQTT connection drops and resubscribes after
  token refresh internally.
- Heartbeat: every `INTERVAL_MINUTES`, log a liveness line and check whether
  the MQTT task (`snoo._mqtt_tasks[serial]`) has silently died; if so, log an
  error and call `start_subscribe` again. Simple insurance for an unattended
  Portainer deployment, not a full watchdog/alerting system.

## Docker / deployment

No Dockerfile changes. `CMD` stays `uv run python -m sync.runner --loop`.
When `SNOO_MODE=live`, `main()` runs `_run_live()` instead, which never
returns — `--loop` is a no-op in that case. Existing `premium`/`basic`
behavior under `--loop` is unchanged.

## Testing / rollout plan

1. Exercise `LiveSessionTracker.handle_event` with synthetic `SnooData`
   objects (constructed to mirror the real device JSON schema) covering:
   session open → several level transitions → close; a session too short to
   pass `MIN_SESSION_MINUTES`; a mid-session process restart (simulated by
   re-instantiating the tracker against the same SQLite file). No pytest
   infra added — project has none; a throwaway script is sufficient given
   this is pure-logic, no I/O.
2. Deploy with `SNOO_MODE=live DRY_RUN=true` against the real account and
   let it run until the next real sleep session, then inspect the logged
   "WOULD WRITE" notes for sanity (segment durations add up, wake-reason
   looks plausible) before flipping `DRY_RUN=false`.
3. Keep `basic` mode available and documented as a fallback in case `live`
   proves unreliable for this account.
