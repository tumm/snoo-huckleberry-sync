"""Tests for sync.config's env-var parsing/validation.

sync.config parses all env vars as module-level statements at import time, so
these tests set required env vars via monkeypatch and reload the module for
each case, rather than testing the process-wide singleton's already-loaded
values.
"""

import importlib

import pytest

import sync.config as config_module

_REQUIRED_ENV = {
    "SNOO_USERNAME": "user@example.com",
    "SNOO_PASSWORD": "pw",
    "HUCKLEBERRY_EMAIL": "user@example.com",
    "HUCKLEBERRY_PASSWORD": "pw",
}


def _reload_with_env(monkeypatch, **overrides):
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    for k, v in overrides.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return importlib.reload(config_module)


@pytest.fixture(autouse=True)
def _restore_config_module():
    yield
    importlib.reload(config_module)  # restore real-environment state for other tests


class TestTimezoneValidation:
    def test_valid_timezone_accepted(self, monkeypatch):
        mod = _reload_with_env(monkeypatch, HUCKLEBERRY_TIMEZONE="Europe/London")
        assert mod.HUCKLEBERRY_TIMEZONE == "Europe/London"

    def test_empty_timezone_raises_friendly_error(self, monkeypatch):
        # Regression test: ZoneInfo("") raises ValueError, not
        # ZoneInfoNotFoundError - _timezone() must catch both (and TypeError)
        # or this crashes with a raw zoneinfo internals message instead of
        # the intended RuntimeError.
        with pytest.raises(RuntimeError, match="not a valid IANA timezone"):
            _reload_with_env(monkeypatch, HUCKLEBERRY_TIMEZONE="")

    def test_garbage_timezone_raises_friendly_error(self, monkeypatch):
        with pytest.raises(RuntimeError, match="not a valid IANA timezone"):
            _reload_with_env(monkeypatch, HUCKLEBERRY_TIMEZONE="Not/AZone")


class TestIntervalMinutesFloor:
    def test_default_accepted(self, monkeypatch):
        mod = _reload_with_env(monkeypatch)
        assert mod.INTERVAL_MINUTES == 15.0

    def test_small_positive_value_accepted(self, monkeypatch):
        # Regression test: the old min_value=1.0 floor rejected sub-minute
        # intervals with no documented justification; only non-positive
        # values should be rejected.
        mod = _reload_with_env(monkeypatch, INTERVAL_MINUTES="0.1")
        assert mod.INTERVAL_MINUTES == 0.1

    def test_zero_rejected(self, monkeypatch):
        with pytest.raises(RuntimeError, match="INTERVAL_MINUTES"):
            _reload_with_env(monkeypatch, INTERVAL_MINUTES="0")

    def test_negative_rejected(self, monkeypatch):
        with pytest.raises(RuntimeError, match="INTERVAL_MINUTES"):
            _reload_with_env(monkeypatch, INTERVAL_MINUTES="-5")
