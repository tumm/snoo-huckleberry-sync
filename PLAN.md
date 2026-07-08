# Plan: Codebase review findings & fixes

Findings from a review of the `sync/` package, ordered by priority. All items implemented.

---

## High-priority (correctness / security) — DONE

### 1. SQLite concurrency race — `dedupe.py`
Added a `threading.Lock` held around every `DedupeStore` method body, so the MQTT
callback thread and fire-and-forget asyncio write tasks can no longer race on the
shared `check_same_thread=False` connection. Verified with a 4-thread × 50-write
concurrency test.

### 2. `ssl=False` disables all TLS verification on Windows — `runner.py`, `find_child_uids.py`
Added `make_aiohttp_connector()` in `ssl_helper.py` which passes the Windows
system-root `SSLContext` from `get_ssl_context()` to `TCPConnector(ssl=...)`,
instead of `ssl=False`. Three call sites consolidated onto the single helper,
removing the duplicated `os.name == "nt"` blocks.

### 3. Firestore `prefs.lastSleep` TOCTOU race — `huckleberry_sink.py`
Wrapped the lastSleep read-modify-write in a Firestore `async_transactional`
transaction so concurrent live-mode writes can no longer race (Firestore retries
on contention automatically).

### 4. `store.close()` never called in live mode — `runner.py`
`_run_live` now wraps `_run_live_loop` in a `try/finally` that closes the store.

### 5. `start_subscribe` not awaited — `runner.py`
Verified against `python-snoo`'s source: `start_subscribe` is synchronous (it
schedules an internal `asyncio.create_task` and returns). Added a clarifying
comment and wrapped it in a `resubscribe()` adapter.

### 6. Duplicate events after reconnect — `live_source.py`
`handle_event` now checks the existing event rows for both `(event_time_ms, state)`
and the back-computed seed timestamp before appending, dropping reconnect
redeliveries instead of duplicating them.

---

## Medium-priority (quality / maintainability) — DONE

### 7. Unit tests — `tests/`
Added 55 tests across `test_formatting.py`, `test_live_source.py`, and
`test_dedupe.py`. Covers `_classify_state`, `_find_soothing_episodes`,
`_infer_wake_reason`, `LiveSessionTracker.handle_event` (seed, append,
duplicate-drop, close, stale-bound, short-discard, summary/detailed notes),
`aggregate_segment_durations`, `fmt_dur`, `format_session_notes`, and
`DedupeStore` (written/active/live + concurrency).

### 8. Doc/code mismatch — `CLAUDE.md`
Fixed: "MD5 hash" → "SHA-256 hash of the SNOO session_id (truncated to 16 hex
chars / 64 bits)".

### 9. `session_builder.py` deleted; `SnooCompletedSession` moved to `models.py`
Dead `SleepInterval` dataclass removed; the shared `SnooCompletedSession` now
lives in `sync/models.py`, imported by `huckleberry_sink`, `live_source`,
`runner`, and `snoo_source`.

### 10. Config validation at load — `config.py`
`HUCKLEBERRY_TIMEZONE` validated as a real IANA zone via `ZoneInfo`.
`HUCKLEBERRY_SLEEP_LOCATION` validated against the allowed set at load (was
only checked at write time). `INTERVAL_MINUTES` / `MIN_SESSION_MINUTES` /
`HISTORY_DAYS` / `IN_PROGRESS_BUFFER_MINUTES` reject negatives. The valid-location
set is exported as `config.VALID_SLEEP_LOCATIONS` and consumed by
`huckleberry_sink`.

### 11. `fetch_past_sessions` refactored — `snoo_source.py`
Duplicated inline aggregation removed; durations now computed once via
`aggregate_segment_durations`. Triple-nested date parse extracted to
`_parse_start_time` with a tuple of formats. 6:00 AM daily window lifted to
`_DAILY_WINDOW_START_HOUR`. Warns when a segment has a non-numeric
`stateDuration` (the computed end time is then a lower bound).

### 12. Private API access wrapped — `snoo_source.py`, `huckleberry_sink.py`
`get_subscription_task(snoo, device)` and `resubscribe(snoo, device, on_message)`
in `snoo_source.py` now wrap `snoo._mqtt_tasks` and `snoo.start_subscribe`,
so `runner.py` no longer reaches into python-snoo's internals directly.
(`huckleberry_sink` still uses `hb._get_firestore_client()` / `hb._get_timezone_offset_minutes()`
since they return the Firestore `AsyncClient` we need for the transaction in #3 —
wrapping them would just move the private access one layer down without removing it.)

### 13. Graceful shutdown in `_run_live` — `runner.py`
Added a SIGTERM/SIGINT `asyncio.Event` that breaks the heartbeat loop, drains
pending write tasks (30s bounded wait, then cancel), disconnects the SNOO MQTT
client, and closes the store in a `finally` block.

### 14. Bounded retry on Huckleberry writes — `runner.py`
`_write_one_live_session` now retries up to 3 times with exponential backoff
(2s, 4s) before giving up and logging the session as lost.

---

## Low-priority (polish) — DONE

### 15. Ruff + mypy config — `pyproject.toml`
`ruff` (E/F/W/I/UP/B/SIM rules, line-length 100, py312 target) and `mypy`
(python 3.12, warn_unused_ignores, warn_redundant_casts) configured. `ruff check`
is clean (remaining intentional late imports marked `# noqa: E402`).

### 19. `__init__.py` — `__version__`
Added `__version__ = "0.1.0"` and a package docstring.

---

## Not implemented (low value, noted for future)

### 16. `HUCKLEBERRY_TIMEZONE` default `America/New_York`
Surprising for a global tool; consider `UTC` or a loud README note. Left as-is
to avoid breaking existing deployments.

### 17. Logging inconsistencies in `find_child_uids.py`
Still uses `print()` instead of `logging`. The script is a one-off interactive
utility, so structured logging adds little value.

### 18. Magic constants
The 6:00 AM daily window was lifted to `_DAILY_WINDOW_START_HOUR`. The
valid-location set was already consolidated into `config.VALID_SLEEP_LOCATIONS`
in #10.

### 20. Windows cert file in shared temp dir (`ssl_helper.py`)
Predictable filename in a shared dir — minor TOCTOU/symlink risk. Left as-is
for compatibility with the existing `get_ssl_context()` which reads the same
file.
