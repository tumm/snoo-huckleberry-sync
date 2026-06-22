# snoo-huckleberry-sync

Syncs completed SNOO sleep sessions into Huckleberry automatically. Polls the SNOO device state every 15 minutes, detects when sessions close, and writes them to Huckleberry. Runs as a single long-running Docker container, suitable for Portainer.

## Safety invariants

- **Never writes to SNOO.** All SNOO access is read-only (HTTP GET only).
- **Only writes to Huckleberry** after you have reviewed a dry run.
- `DRY_RUN=true` by default. Flip it consciously.

## Local setup

```bash
cp .env.example .env
# Edit .env with your credentials
uv sync
```

## Validate before enabling writes

1. Run a pass while a SNOO session is active. It should log "Tracking new SNOO session":
   ```bash
   uv run python -m sync.runner
   ```

2. After the session ends, run again. It should log "WOULD WRITE: HH:MM -> HH:MM (X min)":
   ```bash
   uv run python -m sync.runner
   ```

3. Verify the times look correct against the SNOO app, then flip `DRY_RUN=false` in `.env` and run once more to write to Huckleberry.

4. Run one more time to confirm idempotency. It should log "No active sessions to close."

## Portainer deploy

1. Copy this repo to your Portainer host (or point Portainer at the git repo).

2. Copy `.env.example` to `.env` and fill in credentials. Leave `DB_PATH` unset; docker-compose overrides it to `/data/dedupe.sqlite` automatically.

3. In Portainer, go to Stacks -> Add stack, point at `docker-compose.yml` (or paste it). Deploy.

4. The container polls every `INTERVAL_MINUTES` and writes sessions to Huckleberry once `DRY_RUN=false`. The SQLite dedupe store persists in the `snoo_data` named volume across restarts.

## Tunables (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `INTERVAL_MINUTES` | 15 | How often to poll the SNOO device |
| `DRY_RUN` | true | Log intended writes without touching Huckleberry |
| `DB_PATH` | ./dedupe.sqlite | SQLite path (overridden to `/data/dedupe.sqlite` in Docker) |
| `HUCKLEBERRY_TIMEZONE` | America/New_York | Timezone for Huckleberry writes |
| `HUCKLEBERRY_CHILD_UID` | (auto) | Override child UID; auto-detected from first child if unset |

## How it works

The SNOO REST API (`/hds/me/v11/devices`) returns the current device state on every poll, including `is_active_session`, `session_id`, and `since_session_start_ms`. The sync tool:

1. When `is_active_session=true` with a new `session_id`: records session start, back-computed from `event_time_ms - since_session_start_ms`
2. Each subsequent poll while active: updates the last-seen wall-clock timestamp
3. When `is_active_session=false`: writes all tracked sessions to Huckleberry using last-seen active time as the end time (accurate to within one poll interval), then marks them done in the SQLite dedupe store

Sessions shorter than 60 seconds are discarded as false starts.

## Notes

- PubNub history storage is disabled on the SNOO account, so there is no way to fetch historical sessions retroactively. The tool only tracks sessions it observes while running.
- The SNOO device API does not update `event_time_ms` continuously during a session; it reflects the last state-change event. End times are therefore approximated from wall-clock poll times.
