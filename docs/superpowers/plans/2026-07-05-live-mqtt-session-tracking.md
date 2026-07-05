# Live MQTT Session Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `SNOO_MODE=live` mode that reconstructs completed SNOO sleep sessions from real-time AWS IoT MQTT push events (the same mechanism the official Home Assistant SNOO integration uses), giving minute-level asleep/soothing breakdown without a SNOO Premium subscription, and make it the new default.

**Architecture:** `python-snoo`'s existing `Snoo.start_subscribe(device, callback)` opens a persistent MQTT connection and pushes a `SnooData` object on every device state transition. A new `LiveSessionTracker` persists each transition to SQLite (append-only, keyed by `session_id`) as it arrives, and reconstructs a completed session (with per-state duration breakdown and a best-effort wake-reason) the moment the device goes inactive. `runner.py` gets a new persistent entrypoint, `_run_live()`, that never returns — replacing the poll loop entirely for this mode.

**Tech Stack:** Python 3.12, `python-snoo` 0.11.0 (already installed, provides `SnooData`/`SnooDevice`/`Snoo.start_subscribe`), `aiohttp`, SQLite (stdlib `sqlite3`), existing `huckleberry-api` write path.

## Global Constraints

- `SNOO_MODE` is one of `"premium" | "basic" | "live"`, default `"live"`; invalid values raise `RuntimeError` at import time, matching `config.py`'s existing `_require`/`_bool`/`_int` style.
- No new required env vars for `live` mode — reuses `SNOO_USERNAME`/`SNOO_PASSWORD`/`HUCKLEBERRY_*`/`MIN_SESSION_MINUTES`/`DB_PATH`/`INTERVAL_MINUTES` (repurposed as the live-mode heartbeat cadence).
- No pytest infra added — this project has none (see `CLAUDE.md`: "There are no tests or linter configs in this project"). Verification uses throwaway scripts with plain `assert` statements, run via `uv run python`, and are not committed.
- Do not mock the live SNOO API or Firestore — verify pure logic with synthetic data, and verify I/O-touching code with short manual dry runs against the real account.
- No Dockerfile changes. `CMD` stays `uv run python -m sync.runner --loop`; `--loop` is ignored when `SNOO_MODE=live`.
- **Known library quirk (verified empirically, must be worked around):** `python-snoo`'s `SnooStateMachine.is_active_session` is typed `bool`, but mashumaro deserializes the device's literal JSON string `"false"` into Python `True` (any non-empty string is truthy) — `is_active_session` is therefore **always `True`** after `SnooData.from_dict()`/`from_json()` and must never be read directly. Use `state_machine.session_id != "0"` as the active/inactive signal instead (same convention `sync/snoo_source.py`'s existing `fetch_device_state`/`SnooDeviceState` already uses for the REST-based basic mode).

---

## File Structure

- `sync/config.py` — modify: replace `SNOO_PREMIUM: bool` with `SNOO_MODE: str`.
- `.env.example` — modify: document `SNOO_MODE`.
- `sync/snoo_source.py` — modify: extract `aggregate_segment_durations`, `format_session_notes`, `back_compute_start_ms` out of the inline logic in `fetch_past_sessions` (pure refactor, zero behavior change) so `live_source.py` can reuse them; add `start_live_subscription()` (new, wraps auth + device resolution + `Snoo.start_subscribe`).
- `sync/dedupe.py` — modify: add `live_session_events` table + `append_live_event`/`get_live_events`/`clear_live_events`/`open_live_session_ids`.
- `sync/live_source.py` — new file: `LiveSessionTracker`, pure session-reconstruction logic (no network/Firestore I/O), independently testable with synthetic `SnooData` objects.
- `sync/runner.py` — modify: add `_run_live()` + `_write_one_live_session()`; `main()` dispatches to it when `SNOO_MODE == "live"`, bypassing `--loop`; `run_once()` updated to read `config.SNOO_MODE` instead of the removed `config.SNOO_PREMIUM`.
- `CLAUDE.md` — modify: document the three modes.

---

### Task 1: Config — `SNOO_MODE`

**Files:**
- Modify: `sync/config.py:52` (replace the `SNOO_PREMIUM` line)
- Modify: `.env.example`

**Interfaces:**
- Produces: `config.SNOO_MODE: str`, one of `"premium" | "basic" | "live"`. Consumed by Tasks 6 and 7.

- [ ] **Step 1: Write the verification script**

```bash
cat > /tmp/snoo_verify_task1.sh <<'SCRIPT'
set -e
cd /Users/tobiasengvall/dev/snoo-huckleberry-sync

echo "-- default (no SNOO_MODE set) should be 'live' --"
uv run python -c "from sync import config; assert config.SNOO_MODE == 'live', config.SNOO_MODE; print('OK: default is live')"

echo "-- explicit basic --"
SNOO_MODE=basic uv run python -c "from sync import config; assert config.SNOO_MODE == 'basic'; print('OK: basic')"

echo "-- explicit premium --"
SNOO_MODE=premium uv run python -c "from sync import config; assert config.SNOO_MODE == 'premium'; print('OK: premium')"

echo "-- invalid value must raise --"
if SNOO_MODE=bogus uv run python -c "from sync import config" 2>/tmp/snoo_verify_task1.err; then
  echo "FAIL: invalid SNOO_MODE did not raise"; exit 1
fi
grep -q "SNOO_MODE" /tmp/snoo_verify_task1.err && echo "OK: invalid value raised RuntimeError mentioning SNOO_MODE"
SCRIPT
chmod +x /tmp/snoo_verify_task1.sh
```

- [ ] **Step 2: Run it to confirm it fails (SNOO_MODE doesn't exist yet)**

Run: `bash /tmp/snoo_verify_task1.sh`
Expected: first command fails with `AttributeError: module 'sync.config' has no attribute 'SNOO_MODE'`.

- [ ] **Step 3: Implement the config change**

In `sync/config.py`, replace the last line (`SNOO_PREMIUM: bool = _bool("SNOO_PREMIUM", False)`) with:

```python
_VALID_SNOO_MODES = {"premium", "basic", "live"}


def _choice(key: str, default: str, choices: set[str]) -> str:
    raw = os.environ.get(key, default)
    if raw not in choices:
        raise RuntimeError(f"Env var {key!r} must be one of {sorted(choices)}, got {raw!r}")
    return raw


SNOO_MODE: str = _choice("SNOO_MODE", "live", _VALID_SNOO_MODES)
```

Note: `_choice` and `_VALID_SNOO_MODES` must be defined before `SNOO_MODE` is assigned, so place this block after the other `_int`/`_bool`/`_float` helper defs (i.e. where `SNOO_PREMIUM`'s line currently is, at the end of the file).

- [ ] **Step 4: Run it to confirm it passes**

Run: `bash /tmp/snoo_verify_task1.sh`
Expected: all four checks print `OK: ...`, no failures.

- [ ] **Step 5: Update `.env.example`**

Replace:
```
# Set to true if this account has a SNOO Premium subscription. Premium mode fetches full
# session history (with soothing/asleep breakdown) from the sessions/daily endpoint.
# Without Premium that endpoint returns no data, so leave this false to poll live device
# state instead and build sessions from session start/end transitions (no breakdown, just
# total duration).
SNOO_PREMIUM=false
```
with:
```
# Which source to use for detecting completed sleep sessions:
#   live    - (default, recommended) persistent AWS IoT MQTT push subscription, same
#             mechanism the official Home Assistant SNOO integration uses. Gives
#             minute-level asleep/soothing breakdown without needing SNOO Premium.
#   basic   - polls device state every INTERVAL_MINUTES. Works without Premium, but only
#             total duration - no breakdown - and up to one poll interval of imprecision.
#   premium - fetches full session history from the sessions/daily endpoint. Requires an
#             active SNOO Premium subscription; without one this endpoint silently
#             returns no data every time.
SNOO_MODE=live
```

- [ ] **Step 6: Clean up and commit**

```bash
rm -f /tmp/snoo_verify_task1.sh /tmp/snoo_verify_task1.err
cd /Users/tobiasengvall/dev/snoo-huckleberry-sync
git add sync/config.py .env.example
git commit -m "Replace SNOO_PREMIUM with SNOO_MODE (premium/basic/live)"
```

---

### Task 2: `snoo_source.py` — extract shared aggregation/notes helpers (pure refactor)

No behavior change for `premium` mode — this only moves existing inline logic into standalone, independently testable functions that `live_source.py` (Task 5) will also call.

**Files:**
- Modify: `sync/snoo_source.py` (the `fetch_past_sessions` per-session loop body, roughly the segment/notes-building section)

**Interfaces:**
- Produces:
  - `aggregate_segment_durations(segments: list[tuple[str, float]]) -> tuple[float, float, dict[str, float]]` — `segments` is `(type_label, duration_seconds)` pairs; returns `(asleep_seconds, soothing_seconds, other_by_label)`. Classification: label containing `"soothing"` (case-insensitive) → soothing bucket; containing `"asleep"` → asleep bucket; otherwise, if the label is truthy, bucketed individually under `other[label]` (original case preserved, exactly matching current behavior).
  - `format_session_notes(asleep_s: float, soothing_s: float, other: dict[str, float], extra_lines: list[str] | None = None) -> str`
  - `back_compute_start_ms(event_time_ms: int, since_session_start_ms: int | None) -> int | None` — returns `None` if `since_session_start_ms` is `None` or negative.
  - Consumed by Task 5 (`live_source.py`) and Task 6.

- [ ] **Step 1: Write the verification script**

```bash
cat > /tmp/snoo_verify_task2.py <<'EOF'
from sync.snoo_source import aggregate_segment_durations, format_session_notes, back_compute_start_ms

# aggregate_segment_durations
asleep, soothing, other = aggregate_segment_durations([
    ("asleep", 600.0), ("soothing", 300.0), ("Crying", 60.0), ("asleep", 120.0),
])
assert asleep == 720.0, asleep
assert soothing == 300.0, soothing
assert other == {"Crying": 60.0}, other

# non-numeric durations are skipped, not counted anywhere
asleep2, soothing2, other2 = aggregate_segment_durations([("asleep", None), ("Weird", "nope")])
assert (asleep2, soothing2, other2) == (0.0, 0.0, {}), (asleep2, soothing2, other2)

# empty label is skipped from `other` even with a numeric duration
asleep3, soothing3, other3 = aggregate_segment_durations([("", 45.0)])
assert (asleep3, soothing3, other3) == (0.0, 0.0, {}), (asleep3, soothing3, other3)

# format_session_notes
notes = format_session_notes(3600.0, 300.0, {"Crying": 60.0})
assert notes == (
    "SNOO Sleep Session Summary:\n"
    "\n- Asleep: 1h\n"
    "\n- Soothing: 5m\n"
    "- Crying: 1m"
), notes

notes_with_extra = format_session_notes(60.0, 0.0, {}, extra_lines=["- Ended: Picked up out of SNOO"])
assert notes_with_extra.endswith("- Ended: Picked up out of SNOO"), notes_with_extra

# back_compute_start_ms
assert back_compute_start_ms(10_000, 4_000) == 6_000
assert back_compute_start_ms(10_000, -1) is None
assert back_compute_start_ms(10_000, None) is None

print("Task 2 verification: ALL PASSED")
EOF
```

- [ ] **Step 2: Run it to confirm it fails (functions don't exist yet)**

Run: `cd /Users/tobiasengvall/dev/snoo-huckleberry-sync && uv run python /tmp/snoo_verify_task2.py`
Expected: `ImportError: cannot import name 'aggregate_segment_durations' from 'sync.snoo_source'`

- [ ] **Step 3: Implement the extraction**

In `sync/snoo_source.py`, add these module-level functions (place them above `fetch_past_sessions`, below the dataclasses):

```python
def back_compute_start_ms(event_time_ms: int, since_session_start_ms: int | None) -> int | None:
    """Back-compute when a session began from a device event's timestamp and its
    reported elapsed-since-start. Returns None if the device hasn't reported a
    valid elapsed value (e.g. -1, meaning no session in progress)."""
    if since_session_start_ms is None or since_session_start_ms < 0:
        return None
    return event_time_ms - since_session_start_ms


def aggregate_segment_durations(
    segments: list[tuple[str, float]]
) -> tuple[float, float, dict[str, float]]:
    """segments: (type_label, duration_seconds) pairs. Returns
    (asleep_seconds, soothing_seconds, other_by_label)."""
    asleep = 0.0
    soothing = 0.0
    other: dict[str, float] = defaultdict(float)
    for seg_type, dur in segments:
        if not isinstance(dur, (int, float)):
            continue
        ll = (seg_type or "").lower()
        if "soothing" in ll:
            soothing += dur
        elif "asleep" in ll:
            asleep += dur
        elif seg_type:
            other[seg_type] += dur
    return asleep, soothing, dict(other)


def _fmt_dur(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    parts = []
    if h > 0:
        parts.append(f"{h}h")
    if m > 0:
        parts.append(f"{m}m")
    if s > 0 or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def format_session_notes(
    asleep_s: float,
    soothing_s: float,
    other: dict[str, float],
    extra_lines: list[str] | None = None,
) -> str:
    lines = [
        "SNOO Sleep Session Summary:",
        f"\n- Asleep: {_fmt_dur(asleep_s)}",
        f"\n- Soothing: {_fmt_dur(soothing_s)}",
    ]
    for label, dur in sorted(other.items()):
        lines.append(f"- {label.capitalize()}: {_fmt_dur(dur)}")
    if extra_lines:
        lines.extend(extra_lines)
    return "\n".join(lines)
```

Then in `fetch_past_sessions`, replace this block:
```python
        def fmt_dur(seconds: float) -> str:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            parts = []
            if h > 0:
                parts.append(f"{h}h")
            if m > 0:
                parts.append(f"{m}m")
            if s > 0 or not parts:
                parts.append(f"{s}s")
            return " ".join(parts)

        notes_lines = [
            "SNOO Sleep Session Summary:",
            f"\n- Asleep: {fmt_dur(asleep_duration)}",
            f"\n- Soothing: {fmt_dur(soothing_duration)}",
        ]
        
        other_states = defaultdict(float)
        for seg in segments:
            seg_type = seg.get("type", "")
            dur = seg.get("stateDuration")
            if seg_type and isinstance(dur, (int, float)):
                other_states[seg_type] += dur

        for stype, sdur in sorted(other_states.items()):
            if stype.lower() not in ("asleep", "soothing"):
                notes_lines.append(f"- {stype.capitalize()}: {fmt_dur(sdur)}")

        notes = "\n".join(notes_lines)
```
with:
```python
        _, _, other_states = aggregate_segment_durations(
            [(seg.get("type", ""), seg.get("stateDuration")) for seg in segments]
        )
        notes = format_session_notes(asleep_duration, soothing_duration, other_states)
```

(The existing `total_duration`/`asleep_duration`/`soothing_duration` accumulation loop right above this block is unchanged — it still computes the same totals independently, since it also needs `total_duration` for `end_dt_utc`, which the new helper doesn't return.)

- [ ] **Step 4: Run it to confirm it passes**

Run: `cd /Users/tobiasengvall/dev/snoo-huckleberry-sync && uv run python /tmp/snoo_verify_task2.py`
Expected: `Task 2 verification: ALL PASSED`

- [ ] **Step 5: Confirm the refactor didn't change premium-mode behavior**

Run: `cd /Users/tobiasengvall/dev/snoo-huckleberry-sync && DRY_RUN=true SNOO_MODE=premium DB_PATH=/tmp/snoo_verify_task2.sqlite uv run python -m sync.runner`
Expected: same as before the refactor — `Fetched 0 completed sessions from SNOO history` (this account has no Premium data), no traceback.

- [ ] **Step 6: Clean up and commit**

```bash
rm -f /tmp/snoo_verify_task2.py /tmp/snoo_verify_task2.sqlite
cd /Users/tobiasengvall/dev/snoo-huckleberry-sync
git add sync/snoo_source.py
git commit -m "Extract shared segment-aggregation/notes helpers in snoo_source.py"
```

---

### Task 3: `snoo_source.py` — live subscription helper

**Files:**
- Modify: `sync/snoo_source.py` (add new function + imports)

**Interfaces:**
- Consumes: `python_snoo.snoo.Snoo`, `python_snoo.containers.SnooDevice`, `python_snoo.containers.SnooData`
- Produces: `async def start_live_subscription(websession: aiohttp.ClientSession, username: str, password: str, on_message: Callable[[SnooData], None]) -> tuple[Snoo, SnooDevice]`. Consumed by Task 6.

- [ ] **Step 1: Write the verification script**

This talks to the real SNOO account (no way to test MQTT wiring without it — matches the project convention of not mocking the live API). It runs for a few seconds, confirms the device resolves and the subscribe call doesn't raise, then exits.

```bash
cat > /tmp/snoo_verify_task3.py <<'EOF'
import asyncio
import aiohttp
from sync import config
from sync.snoo_source import start_live_subscription

received = []

async def main():
    async with aiohttp.ClientSession() as session:
        snoo, device = await start_live_subscription(
            session, config.SNOO_USERNAME, config.SNOO_PASSWORD, received.append
        )
        assert device.serialNumber, "device has no serial number"
        assert device.awsIoT is not None, "device has no awsIoT info, can't subscribe"
        print(f"OK: subscribed to device {device.serialNumber} ({device.name})")
        await asyncio.sleep(5)  # give the MQTT connection a moment to establish
        task = snoo._mqtt_tasks.get(device.serialNumber)
        assert task is not None, "no MQTT task was created"
        assert not task.done(), f"MQTT task exited early: {task.exception() if task.done() else None}"
        print("OK: MQTT task is alive after 5s")
        task.cancel()
        if snoo.reauth_task:
            snoo.reauth_task.cancel()

asyncio.run(main())
print("Task 3 verification: ALL PASSED")
EOF
```

- [ ] **Step 2: Run it to confirm it fails (function doesn't exist yet)**

Run: `cd /Users/tobiasengvall/dev/snoo-huckleberry-sync && uv run python /tmp/snoo_verify_task3.py`
Expected: `ImportError: cannot import name 'start_live_subscription' from 'sync.snoo_source'`

- [ ] **Step 3: Implement `start_live_subscription`**

Add to `sync/snoo_source.py`, near the top, add these imports alongside the existing ones:
```python
from typing import Callable

from python_snoo.containers import SnooData, SnooDevice
```

Then add the function (after `fetch_device_state`):
```python
async def start_live_subscription(
    websession: aiohttp.ClientSession,
    username: str,
    password: str,
    on_message: Callable[[SnooData], None],
) -> tuple[Snoo, SnooDevice]:
    """Authenticate, resolve the account's first SNOO device, and start a
    persistent AWS IoT MQTT subscription delivering live state-transition
    events - the same mechanism the official Home Assistant SNOO integration
    uses (see homeassistant/components/snoo/coordinator.py upstream).

    Returns (snoo, device) so the caller can run a heartbeat/resubscribe
    watchdog (checking snoo._mqtt_tasks[device.serialNumber] and calling
    snoo.start_subscribe(device, on_message) again if it died) and keep the
    Snoo instance alive for its automatic token-refresh/resubscription.
    """
    snoo = Snoo(username, password, websession)
    await snoo.authorize()

    devices = await snoo.get_devices()
    if not devices:
        raise RuntimeError("No SNOO devices found on this account")
    device = devices[0]
    log.info("Live mode tracking device %s (%s)", device.serialNumber, device.name)

    snoo.start_subscribe(device, on_message)
    await snoo.get_status(device)
    return snoo, device
```

- [ ] **Step 4: Run it to confirm it passes**

Run: `cd /Users/tobiasengvall/dev/snoo-huckleberry-sync && uv run python /tmp/snoo_verify_task3.py`
Expected: `OK: subscribed to device ...`, `OK: MQTT task is alive after 5s`, `Task 3 verification: ALL PASSED`

- [ ] **Step 5: Clean up and commit**

```bash
rm -f /tmp/snoo_verify_task3.py
cd /Users/tobiasengvall/dev/snoo-huckleberry-sync
git add sync/snoo_source.py
git commit -m "Add start_live_subscription helper for AWS IoT MQTT push events"
```

---

### Task 4: `dedupe.py` — live session event tracking

**Files:**
- Modify: `sync/dedupe.py`

**Interfaces:**
- Produces:
  - `DedupeStore.append_live_event(session_id: str, event_time_ms: int, state: str) -> None`
  - `DedupeStore.get_live_events(session_id: str) -> list[tuple[int, str]]` (ordered oldest-first)
  - `DedupeStore.clear_live_events(session_id: str) -> None`
  - `DedupeStore.open_live_session_ids() -> list[str]` (oldest-opened session first)
  - Consumed by Task 5 (`live_source.py`).

- [ ] **Step 1: Write the verification script**

```bash
cat > /tmp/snoo_verify_task4.py <<'EOF'
import os
import tempfile
from sync.dedupe import DedupeStore

path = tempfile.mktemp(suffix=".sqlite")
store = DedupeStore(path)

assert store.open_live_session_ids() == []
assert store.get_live_events("sess1") == []

store.append_live_event("sess1", 1000, "BASELINE")
store.append_live_event("sess1", 2000, "LEVEL1")
assert store.get_live_events("sess1") == [(1000, "BASELINE"), (2000, "LEVEL1")], store.get_live_events("sess1")
assert store.open_live_session_ids() == ["sess1"]

store.append_live_event("sess2", 500, "BASELINE")
assert store.open_live_session_ids() == ["sess1", "sess2"], store.open_live_session_ids()

store.clear_live_events("sess1")
assert store.get_live_events("sess1") == []
assert store.open_live_session_ids() == ["sess2"]

store.close()

# Re-open the same file (simulates a process restart) and confirm sess2's
# events survived.
store2 = DedupeStore(path)
assert store2.get_live_events("sess2") == [(500, "BASELINE")]
store2.close()

os.remove(path)
print("Task 4 verification: ALL PASSED")
EOF
```

- [ ] **Step 2: Run it to confirm it fails (methods don't exist yet)**

Run: `cd /Users/tobiasengvall/dev/snoo-huckleberry-sync && uv run python /tmp/snoo_verify_task4.py`
Expected: `AttributeError: 'DedupeStore' object has no attribute 'append_live_event'`

- [ ] **Step 3: Implement the table and methods**

In `sync/dedupe.py`, update the module docstring and `_DDL`:
```python
"""SQLite-backed idempotency store.

written_sessions    - sessions already synced to Huckleberry (permanent).
active_sessions     - sessions currently in progress on the SNOO (transient; basic mode only).
live_session_events - per-state-transition event log for in-progress sessions (transient; live mode only).
"""
```
Add to `_DDL` (after the existing `active_sessions` table):
```sql
CREATE TABLE IF NOT EXISTS live_session_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    event_time_ms INTEGER NOT NULL,
    state         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_session_events_session_id ON live_session_events(session_id);
```

Add these methods to `DedupeStore` (after the `active_sessions` methods, before `close`):
```python
    # ---- live session event tracking (live MQTT mode) ----

    def append_live_event(self, session_id: str, event_time_ms: int, state: str) -> None:
        self._conn.execute(
            "INSERT INTO live_session_events (session_id, event_time_ms, state) VALUES (?, ?, ?)",
            (session_id, event_time_ms, state),
        )
        self._conn.commit()

    def get_live_events(self, session_id: str) -> list[tuple[int, str]]:
        """Return (event_time_ms, state) rows for a session, oldest first."""
        cur = self._conn.execute(
            "SELECT event_time_ms, state FROM live_session_events WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        )
        return cur.fetchall()

    def clear_live_events(self, session_id: str) -> None:
        self._conn.execute("DELETE FROM live_session_events WHERE session_id = ?", (session_id,))
        self._conn.commit()

    def open_live_session_ids(self) -> list[str]:
        """Distinct session_ids currently being tracked, oldest-opened first."""
        cur = self._conn.execute(
            "SELECT session_id FROM live_session_events GROUP BY session_id ORDER BY MIN(id)"
        )
        return [row[0] for row in cur.fetchall()]
```

- [ ] **Step 4: Run it to confirm it passes**

Run: `cd /Users/tobiasengvall/dev/snoo-huckleberry-sync && uv run python /tmp/snoo_verify_task4.py`
Expected: `Task 4 verification: ALL PASSED`

- [ ] **Step 5: Clean up and commit**

```bash
rm -f /tmp/snoo_verify_task4.py
cd /Users/tobiasengvall/dev/snoo-huckleberry-sync
git add sync/dedupe.py
git commit -m "Add live_session_events tracking table to dedupe.py"
```

---

### Task 5: `sync/live_source.py` — session reconstruction logic

Pure logic, no network/Firestore I/O — fully testable with synthetic events.

**Files:**
- Create: `sync/live_source.py`

**Interfaces:**
- Consumes: `DedupeStore` (Task 4), `SnooCompletedSession`/`aggregate_segment_durations`/`format_session_notes`/`back_compute_start_ms` (Task 2), `python_snoo.containers.SnooData`/`SnooEvents`
- Produces: `LiveSessionTracker(store: DedupeStore, min_session_seconds: float)` with `handle_event(data: SnooData) -> list[SnooCompletedSession]` (empty list if no session closed). Consumed by Task 6.

- [ ] **Step 1: Write the verification script**

```bash
cat > /tmp/snoo_verify_task5.py <<'EOF'
import os
import tempfile
from python_snoo.containers import SnooData
from sync.dedupe import DedupeStore
from sync.live_source import LiveSessionTracker

def make_event(event_time_ms, session_id, state, since_session_start_ms=-1,
               left_clip=1, right_clip=1, event="activity"):
    return SnooData.from_dict({
        "left_safety_clip": left_clip, "rx_signal": {"rssi": -46, "strength": 97},
        "right_safety_clip": right_clip, "sw_version": "v1.15.07",
        "event_time_ms": event_time_ms,
        "state_machine": {
            "up_transition": "NONE", "since_session_start_ms": since_session_start_ms,
            "sticky_white_noise": "off", "weaning": "off", "time_left": -1,
            "session_id": session_id, "state": state, "is_active_session": "true" if session_id != "0" else "false",
            "down_transition": "NONE", "hold": "off", "audio": "on",
        },
        "system_state": "normal", "event": event,
    })

path = tempfile.mktemp(suffix=".sqlite")
store = DedupeStore(path)
tracker = LiveSessionTracker(store, min_session_seconds=60)

# 1. Session opens: first event for a new session_id, since_session_start_ms=0
#    means the session started exactly at this event's timestamp.
t0 = 1_000_000
result = tracker.handle_event(make_event(t0, "sess-a", "BASELINE", since_session_start_ms=0))
assert result == [], result

# 2. A soothing escalation 10 minutes later.
result = tracker.handle_event(make_event(t0 + 600_000, "sess-a", "LEVEL2"))
assert result == [], result

# 3. Back to baseline 5 minutes later.
result = tracker.handle_event(make_event(t0 + 900_000, "sess-a", "BASELINE"))
assert result == [], result

# 4. Session ends 30 minutes after that (safety clip released -> picked up).
end_ms = t0 + 900_000 + 1_800_000
result = tracker.handle_event(make_event(end_ms, "0", "ONLINE", left_clip=0, right_clip=1))
assert len(result) == 1, result
sess = result[0]
assert sess.session_id == "sess-a"
assert abs(sess.total_seconds - 2700.0) < 0.01, sess.total_seconds  # 45 minutes total
assert "Asleep:" in sess.notes and "Soothing:" in sess.notes, sess.notes
assert "Level2" in sess.notes, sess.notes
assert "Picked up out of SNOO" in sess.notes, sess.notes
print("OK: full session reconstructed with breakdown and wake reason")

# Store should have no lingering events for this session now.
assert store.open_live_session_ids() == []

# 5. A too-short session gets discarded (min_session_seconds=60).
t1 = 2_000_000
tracker.handle_event(make_event(t1, "sess-b", "BASELINE", since_session_start_ms=0))
result = tracker.handle_event(make_event(t1 + 5_000, "0", "ONLINE"))  # only 5s long
assert result == [], result
print("OK: too-short session discarded")

# 6. Restart resilience: open a session, then simulate a process restart by
#    creating a brand new tracker against the same DB file mid-session.
t2 = 3_000_000
tracker.handle_event(make_event(t2, "sess-c", "BASELINE", since_session_start_ms=0))
tracker2 = LiveSessionTracker(store, min_session_seconds=60)
result = tracker2.handle_event(make_event(t2 + 120_000, "0", "ONLINE"))
assert len(result) == 1, result
assert result[0].session_id == "sess-c"
assert abs(result[0].total_seconds - 120.0) < 0.01, result[0].total_seconds
print("OK: mid-session restart resumes from persisted events")

store.close()
os.remove(path)
print("Task 5 verification: ALL PASSED")
EOF
```

- [ ] **Step 2: Run it to confirm it fails (module doesn't exist yet)**

Run: `cd /Users/tobiasengvall/dev/snoo-huckleberry-sync && uv run python /tmp/snoo_verify_task5.py`
Expected: `ModuleNotFoundError: No module named 'sync.live_source'`

- [ ] **Step 3: Implement `sync/live_source.py`**

```python
"""Reconstructs completed SNOO sleep sessions from live MQTT push events.

Pure logic, no network/Firestore I/O - sync/runner.py wires this to
python-snoo's live subscription and to Huckleberry writes.

IMPORTANT: python-snoo's SnooStateMachine.is_active_session is always True
after deserialization (mashumaro coerces the device's literal "false" string
into a truthy Python bool) - do not read it. session_id == "0" is the
reliable inactive signal, matching sync/snoo_source.py's REST-based
SnooDeviceState convention.
"""

import logging
from datetime import datetime, timezone

from python_snoo.containers import SnooData, SnooEvents

from .dedupe import DedupeStore
from .snoo_source import (
    SnooCompletedSession,
    aggregate_segment_durations,
    back_compute_start_ms,
    format_session_notes,
)

log = logging.getLogger(__name__)

_ASLEEP_STATES = {"BASELINE", "WEANING_BASELINE"}
_SOOTHING_STATES = {"LEVEL1", "LEVEL2", "LEVEL3", "LEVEL4"}


def _classify_state(state: str) -> tuple[str, str | None]:
    """Map a raw device state to (aggregation bucket label, optional individual
    sub-label). Soothing levels count toward the "soothing" summary total AND
    keep their own label (e.g. "Level2") so they also show as their own line."""
    s = (state or "").upper()
    if s in _ASLEEP_STATES:
        return "asleep", None
    if s in _SOOTHING_STATES:
        return "soothing", s.capitalize()
    return (state.capitalize() if state else "Unknown"), None


def _infer_wake_reason(events: list[tuple[int, str]], closing_data: SnooData) -> str | None:
    """Best-effort guess at how the session ended, from the closing push event.
    Unvalidated against real sessions - expect to refine after real data lands."""
    last_state = events[-1][1].upper() if events else ""
    if closing_data.left_safety_clip == 0 or closing_data.right_safety_clip == 0:
        return "Picked up out of SNOO"
    if last_state == "TIMEOUT":
        return "Soothing timed out without settling"
    if last_state == "SUSPENDED":
        return "Session stopped manually"
    if closing_data.event == SnooEvents.CRY:
        return "Ended after sustained crying"
    return None


class LiveSessionTracker:
    """Tracks in-progress SNOO sessions across live push events, persisting
    each state transition to SQLite as it arrives so a process restart
    mid-session resumes from the persisted events instead of losing them."""

    def __init__(self, store: DedupeStore, min_session_seconds: float) -> None:
        self._store = store
        self._min_session_seconds = min_session_seconds

    def handle_event(self, data: SnooData) -> list[SnooCompletedSession]:
        session_id = str(data.state_machine.session_id)
        if session_id in ("0", ""):
            return self._close_open_sessions(data)

        state = str(data.state_machine.state)
        existing = self._store.get_live_events(session_id)
        if not existing:
            start_ms = back_compute_start_ms(
                data.event_time_ms, data.state_machine.since_session_start_ms
            )
            if start_ms is None:
                start_ms = data.event_time_ms
            self._store.append_live_event(session_id, start_ms, state)
            log.info(
                "Tracking new live SNOO session %s (started %s)",
                session_id,
                datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
            )
        else:
            self._store.append_live_event(session_id, data.event_time_ms, state)
        return []

    def _close_open_sessions(self, closing_data: SnooData) -> list[SnooCompletedSession]:
        completed: list[SnooCompletedSession] = []
        for session_id in self._store.open_live_session_ids():
            events = self._store.get_live_events(session_id)
            self._store.clear_live_events(session_id)

            if not events:
                log.warning("Live session %s had no recorded events, discarding.", session_id)
                continue

            start_ms = events[0][0]
            end_ms = closing_data.event_time_ms
            total_seconds = (end_ms - start_ms) / 1000
            if total_seconds < self._min_session_seconds:
                log.info("Live session %s too short (%.0fs) - discarding.", session_id, total_seconds)
                continue

            segments: list[tuple[str, float]] = []
            for i, (t_ms, state) in enumerate(events):
                next_ms = events[i + 1][0] if i + 1 < len(events) else end_ms
                dur = (next_ms - t_ms) / 1000
                bucket_label, sub_label = _classify_state(state)
                segments.append((bucket_label, dur))
                if sub_label:
                    segments.append((sub_label, dur))

            asleep_s, soothing_s, other = aggregate_segment_durations(segments)
            wake_reason = _infer_wake_reason(events, closing_data)
            extra_lines = [f"- Ended: {wake_reason}"] if wake_reason else None
            notes = format_session_notes(asleep_s, soothing_s, other, extra_lines=extra_lines)

            completed.append(SnooCompletedSession(
                session_id=session_id,
                start=datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc),
                end=datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc),
                total_seconds=total_seconds,
                notes=notes,
            ))
        return completed
```

- [ ] **Step 4: Run it to confirm it passes**

Run: `cd /Users/tobiasengvall/dev/snoo-huckleberry-sync && uv run python /tmp/snoo_verify_task5.py`
Expected: five `OK: ...` lines then `Task 5 verification: ALL PASSED`

- [ ] **Step 5: Clean up and commit**

```bash
rm -f /tmp/snoo_verify_task5.py
cd /Users/tobiasengvall/dev/snoo-huckleberry-sync
git add sync/live_source.py
git commit -m "Add LiveSessionTracker for live-mode session reconstruction"
```

---

### Task 6: `runner.py` — persistent live mode + dispatch

**Files:**
- Modify: `sync/runner.py`

**Interfaces:**
- Consumes: `start_live_subscription` (Task 3), `LiveSessionTracker` (Task 5), `config.SNOO_MODE` (Task 1), existing `write_sleep_interval`/`make_huckleberry_client`/`resolve_child_uid`/`DedupeStore`.
- Produces: `_run_live()` (new), `main()` dispatches to it when `config.SNOO_MODE == "live"`.

- [ ] **Step 1: Update imports and remove the `SNOO_PREMIUM` reference**

In `sync/runner.py`, update the imports:
```python
from .snoo_source import fetch_past_sessions, fetch_device_state, start_live_subscription, SnooCompletedSession
from .live_source import LiveSessionTracker
```
Add near the top (alongside the other imports):
```python
from python_snoo.containers import SnooData
```

- [ ] **Step 2: Replace the `run_once`/mode-dispatch logic to read `SNOO_MODE`**

Replace:
```python
async def run_once() -> None:
    dry = config.DRY_RUN
    mode = "premium" if config.SNOO_PREMIUM else "basic (device-polling)"
    log.info("Starting sync pass (DRY_RUN=%s, mode=%s, interval=%.0fmin)", dry, mode, config.INTERVAL_MINUTES)

    store = DedupeStore(config.DB_PATH)

    import os
    connector = aiohttp.TCPConnector(ssl=False) if os.name == "nt" else None
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            if config.SNOO_PREMIUM:
                await _run_once_premium(session, store, dry)
            else:
                await _run_once_basic(session, store, dry)
    finally:
        store.close()
```
with:
```python
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
```
(`run_once`/`run_loop` are only ever reached for `premium`/`basic` now - `main()` intercepts `live` before this point, see Step 4.)

- [ ] **Step 3: Add `_write_one_live_session` and `_run_live`**

Add after `_run_once_basic`:
```python
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

    await write_sleep_interval(hb, child_uid, sess)
    store.mark(sess.session_id, sess.start, sess.end)
    log.info("Live session %s written to Huckleberry.", sess.session_id)


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

        def on_message(data: SnooData) -> None:
            try:
                completed = tracker.handle_event(data)
            except Exception:
                log.error("Error handling live event, dropping it.", exc_info=True)
                return
            for sess in completed:
                asyncio.create_task(_write_one_live_session(store, hb, child_uid, sess, dry))

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
            else:
                log.info("Live mode heartbeat: MQTT subscription alive for %s.", device.serialNumber)
```

- [ ] **Step 4: Dispatch to `_run_live()` from `main()`**

Replace:
```python
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
```
with:
```python
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
```

- [ ] **Step 5: Verify basic/premium modes still work (regression check)**

Run:
```bash
cd /Users/tobiasengvall/dev/snoo-huckleberry-sync
uv run python -c "import sync.runner" && echo "IMPORT OK"
DRY_RUN=true SNOO_MODE=basic DB_PATH=/tmp/snoo_verify_task6a.sqlite uv run python -m sync.runner
DRY_RUN=true SNOO_MODE=premium DB_PATH=/tmp/snoo_verify_task6b.sqlite uv run python -m sync.runner
```
Expected: import succeeds; both runs log `Starting sync pass (DRY_RUN=True, mode=basic, ...)` / `mode=premium` respectively and complete without traceback (same behavior as before this task, just reading `SNOO_MODE` instead of `SNOO_PREMIUM`).

- [ ] **Step 6: Verify live mode connects and the heartbeat/watchdog loop runs**

Use a short heartbeat interval so the watchdog fires quickly, and a timeout so the persistent process doesn't run forever:
```bash
cd /Users/tobiasengvall/dev/snoo-huckleberry-sync
DRY_RUN=true SNOO_MODE=live INTERVAL_MINUTES=0.05 DB_PATH=/tmp/snoo_verify_task6c.sqlite timeout 15 uv run python -m sync.runner || true
```
Expected output includes, in order:
- `Starting live mode (DRY_RUN=True) - persistent MQTT session tracking.`
- `Live mode tracking device <serial> (<name>)`
- at least one `Live mode heartbeat: MQTT subscription alive for <serial>.` (heartbeat fires every 3s at `INTERVAL_MINUTES=0.05`)
- no traceback (the process is killed by `timeout`, which is expected and not an error - hence `|| true`)

- [ ] **Step 7: Clean up and commit**

```bash
rm -f /tmp/snoo_verify_task6a.sqlite /tmp/snoo_verify_task6b.sqlite /tmp/snoo_verify_task6c.sqlite
cd /Users/tobiasengvall/dev/snoo-huckleberry-sync
git add sync/runner.py
git commit -m "Add persistent live mode (_run_live) with heartbeat/resubscribe watchdog"
```

---

### Task 7: Docs + final rollout

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `CLAUDE.md`'s Architecture section**

Replace the two-mode bullet list (premium/basic) with three modes:
```markdown
- **Live mode** (`SNOO_MODE=live`, default): `snoo_source.start_live_subscription` opens a persistent AWS IoT MQTT push subscription (the same mechanism the official Home Assistant SNOO integration uses - confirmed by reading its source). `runner._run_live()` never returns; `live_source.LiveSessionTracker` reconstructs completed sessions from real-time state transitions (persisted to SQLite as they arrive, so a restart mid-session resumes rather than loses data), giving a minute-level asleep/soothing/other-state breakdown plus a best-effort wake-reason guess - all without needing SNOO Premium. `--loop` is ignored in this mode since the connection itself never returns.
- **Basic mode** (`SNOO_MODE=basic`): polls `/hds/me/v11/devices` every `INTERVAL_MINUTES` and reconstructs sessions from `is_active_session` transitions across polls (kept as a fallback). No breakdown, only total duration.
- **Premium mode** (`SNOO_MODE=premium`): `snoo_source.fetch_past_sessions` calls `/ss/me/v11/babies/{baby_id}/sessions/daily` for full session history with a soothing/asleep breakdown. **Requires an active SNOO Premium subscription** - without one it returns `200` with empty `levels: []` regardless of real activity (confirmed 2026-07-04).
```

Update the module responsibilities table row for `sync/snoo_source.py` and `sync/dedupe.py`:
```markdown
| `sync/snoo_source.py` | Authenticates with `python-snoo`; `fetch_past_sessions` (premium), `fetch_device_state` (basic polling), `start_live_subscription` (live MQTT push); shared `aggregate_segment_durations`/`format_session_notes` helpers |
| `sync/dedupe.py` | SQLite store with three tables: `written_sessions` (permanent seen-cache), `active_sessions` (transient, basic-mode in-progress tracking), `live_session_events` (transient, live-mode per-transition event log) |
```
Add a row:
```markdown
| `sync/live_source.py` | `LiveSessionTracker` - pure session-reconstruction logic from live MQTT events, no network/Firestore I/O |
```

- [ ] **Step 2: Commit the docs**

```bash
cd /Users/tobiasengvall/dev/snoo-huckleberry-sync
git add CLAUDE.md
git commit -m "Document live MQTT mode in CLAUDE.md"
```

- [ ] **Step 3: Deploy dry-run and wait for a real session (manual, not a code step)**

This is a manual follow-up outside the commit cycle: deploy with `SNOO_MODE=live DRY_RUN=true` (the new defaults - no `.env` change needed beyond removing any leftover `SNOO_PREMIUM` line) and watch the logs until the next real sleep session happens, to sanity-check the notes output (segment durations add up, wake-reason looks plausible) before switching `DRY_RUN=false`.
