# snoo-huckleberry-sync

Automatically syncs completed [SNOO](https://www.happiestbaby.com/pages/snoo) smart bassinet sleep sessions into the [Huckleberry](https://huckleberrycare.com/) baby tracker. It connects directly to Huckleberry's Google Firebase Firestore database using the official Google Cloud SDK and parses SNOO's daily history logs to write precise sleep intervals.

This tool can be run locally as a Python script, scheduled as a task/cron job, or deployed in a Docker container.

## How it works

Every 15 minutes, the script retrieves completed sleep sessions from the SNOO daily history API. Timestamps are resolved directly from Happiest Baby's historical logs down to the millisecond, resulting in 100% precise sleep logs in Huckleberry. 

Each sync session calculates the exact sleep metrics (asleep vs. active soothing durations) and saves it into Huckleberry's notes. Synced sessions are cached in a local SQLite database to ensure they are never written twice.

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

   You should see a sync pass logged every 15 minutes. After your baby's next SNOO session ends, a line like `Wrote sleep interval ... to Huckleberry` confirms it's working.

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
| `INTERVAL_MINUTES`           | No       | `15`                  | How often to poll the SNOO                         |
| `MIN_SESSION_MINUTES`        | No       | `1`                   | Discard sleep sessions shorter than this threshold |
| `HISTORY_DAYS`               | No       | `2`                   | Number of days of SNOO history to sync             |
| `DRY_RUN`                    | No       | `false`               | Log intended writes without touching Huckleberry   |
| `DB_PATH`                    | No       | `/data/dedupe.sqlite` | Path to the SQLite dedupe store                    |

## Safety

- Never writes to SNOO. All SNOO access is read-only (HTTP GET only).
- Set `DRY_RUN=true` to log intended writes without touching Huckleberry.
- Sessions shorter than the configured threshold (default is 1 minute) are discarded as noise.
- The SQLite dedupe store ensures each session is written to Huckleberry exactly once, even if the container restarts.
