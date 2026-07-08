"""Runtime configuration loaded from .env file."""

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Required env var {key!r} is not set. See .env.example.")
    return val


def _float(key: str, default: float, *, min_value: float | None = None) -> float:
    raw = os.environ.get(key, str(default))
    try:
        v = float(raw)
    except ValueError:
        raise RuntimeError(f"Env var {key!r} must be a number, got {raw!r}") from None
    if min_value is not None and v < min_value:
        raise RuntimeError(f"Env var {key!r} must be >= {min_value}, got {v}")
    return v


def _bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "true" if default else "false").lower()
    return v in ("1", "true", "yes")


def _int(key: str, default: int, *, min_value: int | None = None) -> int:
    raw = os.environ.get(key, str(default))
    try:
        v = int(raw)
    except ValueError:
        raise RuntimeError(f"Env var {key!r} must be an integer, got {raw!r}") from None
    if min_value is not None and v < min_value:
        raise RuntimeError(f"Env var {key!r} must be >= {min_value}, got {v}")
    return v


def _choice(key: str, default: str, choices: set[str]) -> str:
    raw = os.environ.get(key, default)
    if raw not in choices:
        raise RuntimeError(f"Env var {key!r} must be one of {sorted(choices)}, got {raw!r}")
    return raw


def _timezone(key: str, default: str) -> str:
    raw = os.environ.get(key, default)
    try:
        ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        raise RuntimeError(
            f"Env var {key!r} is not a valid IANA timezone, got {raw!r}. "
            f"Examples: 'America/New_York', 'Europe/London', 'UTC'."
        ) from None
    return raw


SNOO_USERNAME: str = _require("SNOO_USERNAME")
SNOO_PASSWORD: str = _require("SNOO_PASSWORD")
SNOO_BABY_ID: str | None = os.environ.get("SNOO_BABY_ID") or None

HUCKLEBERRY_EMAIL: str = _require("HUCKLEBERRY_EMAIL")
HUCKLEBERRY_PASSWORD: str = _require("HUCKLEBERRY_PASSWORD")
HUCKLEBERRY_TIMEZONE: str = _timezone("HUCKLEBERRY_TIMEZONE", "America/New_York")
HUCKLEBERRY_CHILD_UID: str | None = os.environ.get("HUCKLEBERRY_CHILD_UID") or None

INTERVAL_MINUTES: float = _float("INTERVAL_MINUTES", 15.0, min_value=1.0)
DRY_RUN: bool = _bool("DRY_RUN", False)
DB_PATH: str = os.environ.get("DB_PATH", "/data/dedupe.sqlite")
MIN_SESSION_MINUTES: int = _int("MIN_SESSION_MINUTES", 1, min_value=0)
HISTORY_DAYS: int = _int("HISTORY_DAYS", 2, min_value=0)
IN_PROGRESS_BUFFER_MINUTES: int = _int("IN_PROGRESS_BUFFER_MINUTES", 5, min_value=0)

VALID_SLEEP_LOCATIONS: frozenset[str] = frozenset({
    "car", "nursing", "wornOrHeld", "stroller", "coSleep",
    "nextToCarer", "onOwnInBed", "bottle", "swing",
})
HUCKLEBERRY_SLEEP_LOCATION: str = _choice(
    "HUCKLEBERRY_SLEEP_LOCATION", "onOwnInBed", set(VALID_SLEEP_LOCATIONS)
)

_VALID_SNOO_MODES = {"premium", "basic", "live"}

if os.environ.get("SNOO_PREMIUM") is not None and os.environ.get("SNOO_MODE") is None:
    raise RuntimeError(
        "SNOO_PREMIUM is deprecated and no longer used. Set SNOO_MODE=premium, "
        "SNOO_MODE=basic, or SNOO_MODE=live (recommended) in .env instead."
    )

SNOO_MODE: str = _choice("SNOO_MODE", "live", _VALID_SNOO_MODES)

_VALID_NOTES_DETAIL = {"summary", "detailed"}
NOTES_DETAIL: str = _choice("NOTES_DETAIL", "summary", _VALID_NOTES_DETAIL)
