# snoo-huckleberry-sync

Automatically syncs completed [SNOO](https://www.happiestbaby.com/pages/snoo) smart bassinet sleep sessions into the [Huckleberry](https://huckleberrycare.com/) baby tracker. It connects directly to Huckleberry's Google Firebase Firestore database using the official Google Cloud SDK, and detects completed SNOO sessions in real time to write precise, minute-level sleep intervals — including an asleep/soothing breakdown — without needing a SNOO Premium subscription.

This tool can be run locally as a Python script, scheduled as a task/cron job, or deployed in a Docker container.

## How it works

There are three modes, set via `SNOO_MODE`:

- **`live`** (default, recommended): opens a persistent real-time connection to your SNOO device (AWS IoT MQTT push events — the same mechanism the official [Home Assistant SNOO integration](https://www.home-assistant.io/integrations/snoo) uses). Every state change (asleep, soothing level 1-4, etc.) is captured the instant it happens, so as soon as a session ends it's reconstructed and written to Huckleberry immediately — no polling delay. Works without a SNOO Premium subscription and gives a full minute-level breakdown: total asleep/soothing time, each individual soothing episode with its time range, and a best-effort guess at how the session ended (picked up, timed out, etc).
- **`basic`**: polls the SNOO device every `INTERVAL_MINUTES` and reconstructs sessions from state changes across polls. Works without Premium, but only reports total sleep duration — no asleep/soothing breakdown — and timestamps are only as precise as your poll interval. Useful as a fallback if `live` mode proves unreliable on your network.
- **`premium`**: fetches full session history (with an asleep/soothing breakdown) from SNOO's own history API. **Requires an active SNOO Premium subscription** — without one, this endpoint silently returns no data.

Synced sessions are cached in a local SQLite database to ensure they are never written twice, even if the container restarts mid-session.

If you have multiple children on your SNOO or Huckleberry accounts, you can run the utility script to find their unique IDs:
```bash
uv run python -m sync.find_child_uids
```
Then use `SNOO_BABY_ID` and `HUCKLEBERRY_CHILD_UID` in your `.env` file to select the correct profiles.

## Local setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
# Edit .env with your credentials
uv sync
uv run python -m sync.runner --loop
```

## Docker Deployment

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/).

1. Clone this repo and enter the directory:

   ```bash
   git clone https://github.com/tumm/snoo-huckleberry-sync.git
   cd snoo-huckleberry-sync
   ```

2. Copy `.env.example` to `.env` and fill in your credentials:

   ```bash
   cp .env.example .env
   ```

3. Start the container:

   ```bash
   docker compose up -d
   ```

4. Check the logs to confirm it's working:

   ```bash
   docker compose logs -f
   ```

   With the default `live` mode, you should see a `Starting live mode` line followed by periodic heartbeat lines confirming the connection is alive. After your baby's next SNOO session ends, a line like `Live session ... written to Huckleberry` confirms it's working. (In `basic`/`premium` mode, you'll instead see a sync pass logged every `INTERVAL_MINUTES`.)

5. To stop: `docker compose down`

The `snoo_data` volume Docker creates persists the SQLite dedupe store across restarts, so sessions are never written twice even if the container is recreated.

> **Tip:** Set `DRY_RUN=true` in `.env` and restart (`docker compose up -d`) to preview what would be written without touching Huckleberry.

## Portainer

In Portainer, go to **Stacks → Add stack**, paste the following, and set the environment variables in the **Env** tab (or use an `.env` file):

```yaml
services:
  snoo-sync:
    image: ghcr.io/tumm/snoo-huckleberry-sync:latest
    restart: unless-stopped
    env_file: .env
    environment:
      DB_PATH: /data/dedupe.sqlite
    volumes:
      - snoo_data:/data

volumes:
  snoo_data:
```



## Environment variables

| Variable                     | Required | Default               | Description                                        |
|------------------------------|----------|-----------------------|----------------------------------------------------|
| `SNOO_USERNAME`              | Yes      |                       | Happiest Baby account email                        |
| `SNOO_PASSWORD`              | Yes      |                       | Happiest Baby account password                     |
| `SNOO_BABY_ID`               | No       | auto-detected         | Override if auto-detection picks the wrong baby    |
| `HUCKLEBERRY_EMAIL`          | Yes      |                       | Huckleberry account email                          |
| `HUCKLEBERRY_PASSWORD`       | Yes      |                       | Huckleberry account password                       |
| `HUCKLEBERRY_TIMEZONE`       | No       | `America/New_York`    | Your local timezone (e.g. `Europe/London`)         |
| `HUCKLEBERRY_CHILD_UID`      | No       | auto-detected         | Override if auto-detection picks the wrong child   |
| `HUCKLEBERRY_SLEEP_LOCATION` | No       | `onOwnInBed`          | Sleep location category tag                        |
| `SNOO_MODE`                  | No       | `live`                | Detection mode: `live`, `basic`, or `premium` (see [How it works](#how-it-works)) |
| `INTERVAL_MINUTES`           | No       | `15`                  | In `basic`/`premium` mode: how often to poll the SNOO. In `live` mode: how often to check the connection is still alive and reconnect if not |
| `MIN_SESSION_MINUTES`        | No       | `1`                   | Discard sleep sessions shorter than this threshold |
| `HISTORY_DAYS`               | No       | `2`                   | `premium` mode only: number of days of SNOO history to sync |
| `IN_PROGRESS_BUFFER_MINUTES` | No       | `5`                   | `premium` mode only: buffer to avoid syncing a session that might still be in progress |
| `DRY_RUN`                    | No       | `false`               | Log intended writes without touching Huckleberry   |
| `DB_PATH`                    | No       | `/data/dedupe.sqlite` | Path to the SQLite dedupe store                    |

## Safety

- Never writes to SNOO. All SNOO access is read-only — HTTP GET in `basic`/`premium` mode, a read-only MQTT subscription in `live` mode.
- Set `DRY_RUN=true` to log intended writes without touching Huckleberry.
- Sessions shorter than the configured threshold (default is 1 minute) are discarded as noise.
- The SQLite dedupe store ensures each session is written to Huckleberry exactly once, even if the container restarts.
- In `live` mode, each state transition is persisted to SQLite as it arrives, so a restart mid-session resumes tracking instead of losing it.
