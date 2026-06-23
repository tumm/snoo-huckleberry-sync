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
    return float(os.environ.get(key, str(default)))


def _bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "true" if default else "false").lower()
    return v in ("1", "true", "yes")


SNOO_USERNAME: str = _require("SNOO_USERNAME")
SNOO_PASSWORD: str = _require("SNOO_PASSWORD")

HUCKLEBERRY_EMAIL: str = _require("HUCKLEBERRY_EMAIL")
HUCKLEBERRY_PASSWORD: str = _require("HUCKLEBERRY_PASSWORD")
HUCKLEBERRY_TIMEZONE: str = os.environ.get("HUCKLEBERRY_TIMEZONE", "America/New_York")
HUCKLEBERRY_CHILD_UID: str | None = os.environ.get("HUCKLEBERRY_CHILD_UID") or None

INTERVAL_MINUTES: float = _float("INTERVAL_MINUTES", 15.0)
DRY_RUN: bool = _bool("DRY_RUN", True)
DB_PATH: str = os.environ.get("DB_PATH", "/data/dedupe.sqlite")
