"""Runtime configuration loaded from environment / .env file."""

import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Required env var {key!r} is not set. See .env.example.")
    return val


def _float(key: str, default: float) -> float:
    raw = os.environ.get(key, str(default))
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"Env var {key!r} must be a number, got {raw!r}") from None


def _bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "true" if default else "false").lower()
    return v in ("1", "true", "yes")


def _int(key: str, default: int) -> int:
    raw = os.environ.get(key, str(default))
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"Env var {key!r} must be an integer, got {raw!r}") from None


SNOO_USERNAME: str = _require("SNOO_USERNAME")
SNOO_PASSWORD: str = _require("SNOO_PASSWORD")
SNOO_BABY_ID: str | None = os.environ.get("SNOO_BABY_ID") or None

HUCKLEBERRY_EMAIL: str = _require("HUCKLEBERRY_EMAIL")
HUCKLEBERRY_PASSWORD: str = _require("HUCKLEBERRY_PASSWORD")
HUCKLEBERRY_TIMEZONE: str = os.environ.get("HUCKLEBERRY_TIMEZONE", "America/New_York")
HUCKLEBERRY_CHILD_UID: str | None = os.environ.get("HUCKLEBERRY_CHILD_UID") or None

INTERVAL_MINUTES: float = _float("INTERVAL_MINUTES", 15.0)
DRY_RUN: bool = _bool("DRY_RUN", False)
DB_PATH: str = os.environ.get("DB_PATH", "/data/dedupe.sqlite")
MIN_SESSION_MINUTES: int = _int("MIN_SESSION_MINUTES", 1)
HUCKLEBERRY_SLEEP_LOCATION: str = os.environ.get("HUCKLEBERRY_SLEEP_LOCATION", "onOwnInBed")
HISTORY_DAYS: int = _int("HISTORY_DAYS", 2)
IN_PROGRESS_BUFFER_MINUTES: int = _int("IN_PROGRESS_BUFFER_MINUTES", 5)
