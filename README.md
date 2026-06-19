# snoo-huckleberry-sync

Syncs completed SNOO sleep sessions into Huckleberry every 15 minutes (6-hour lookback). Runs as a single Docker container, suitable for Portainer.

## Safety invariants

- **Never writes to SNOO.** All SNOO access is read-only (PubNub history + HTTP GET).
- **Only writes to Huckleberry** after you have reviewed a dry run.
- `DRY_RUN=true` by default — flip it consciously.

## Setup

```bash
cp .env.example .env
# Edit .env with your credentials
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run the diagnostic first

```bash
.venv/bin/python snoo_diagnostics.py
```

This is read-only. It confirms which SNOO data path carries the per-session timeline and prints the raw PubNub `ActivityState` events so you can verify the level mapping before any sync code runs.

## Dry run

```bash
DRY_RUN=true .venv/bin/python -m sync.runner
```

## Docker / Portainer

```bash
docker compose up -d
```

The container loops every `INTERVAL_MINUTES`. Mount a volume at `/data` for the SQLite dedupe store.

## Tunables (all in `.env`)

| Variable | Default | Purpose |
|---|---|---|
| `LOOKBACK_HOURS` | 6 | How far back to look for sessions each pass |
| `INTERVAL_MINUTES` | 15 | Polling interval |
| `MERGE_GAP_MINUTES` | 5 | Awake gaps shorter than this are bridged into one sleep interval |
| `MIN_ASLEEP_MINUTES` | 5 | Interval must have at least this much actual asleep time |
| `ASLEEP_RATIO` | 0.5 | Asleep must be at least this fraction of total interval |
| `DRY_RUN` | true | Log intended writes without touching Huckleberry |
| `DB_PATH` | /data/dedupe.sqlite | SQLite dedupe store |
