# snoo-huckleberry-sync

Automatically syncs completed [SNOO](https://www.happiestbaby.com/pages/snoo) smart bassinet sleep sessions into the [Huckleberry](https://huckleberrycare.com/) baby tracker. Runs as a Docker container that polls the SNOO every 15 minutes and writes closed sessions to Huckleberry.

> **Note:** This uses unofficial, reverse-engineered APIs for both SNOO and Huckleberry. It may break if either app updates its backend.

## How it works

Every 15 minutes the container polls the SNOO device API. When it detects a session has started it records the start time; when the session closes it writes the interval to Huckleberry and marks it as done in a local SQLite database so it is never written twice.

End times are approximated from the last poll that saw the session active, so they are accurate to within one poll interval (15 minutes by default).

## Quick start (Docker Desktop)

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

## Local setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
# Edit .env with your credentials
uv sync
uv run python -m sync.runner --loop
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SNOO_USERNAME` | Yes | | Happiest Baby account email |
| `SNOO_PASSWORD` | Yes | | Happiest Baby account password |
| `HUCKLEBERRY_EMAIL` | Yes | | Huckleberry account email |
| `HUCKLEBERRY_PASSWORD` | Yes | | Huckleberry account password |
| `HUCKLEBERRY_TIMEZONE` | No | `America/New_York` | Your local timezone (e.g. `Europe/London`) |
| `HUCKLEBERRY_CHILD_UID` | No | auto-detected | Override if auto-detection picks the wrong child |
| `INTERVAL_MINUTES` | No | `15` | How often to poll the SNOO |
| `DRY_RUN` | No | `false` | Log intended writes without touching Huckleberry |
| `DB_PATH` | No | `/data/dedupe.sqlite` | Path to the SQLite dedupe store |

## Safety

- Never writes to SNOO. All SNOO access is read-only (HTTP GET only).
- Set `DRY_RUN=true` to log intended writes without touching Huckleberry.
- Sessions shorter than 60 seconds are discarded as noise.
- The SQLite dedupe store ensures each session is written to Huckleberry exactly once, even if the container restarts mid-session.
