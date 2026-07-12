"""Regression tests for write_sleep_interval's non-transactional Firestore writes.

Huckleberry's Firebase backend rejects ``beginTransaction`` with 403 for the
user-token credentials this tool uses, so writes must be plain ``.set()`` calls
(no transaction). Fakes stand in for google-cloud-firestore's AsyncClient.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from sync.huckleberry_sink import write_sleep_interval
from sync.models import SnooCompletedSession


class FakeSnapshot:
    def __init__(self, exists, data=None):
        self.exists = exists
        self._data = data or {}

    def to_dict(self):
        return self._data


class FakeSubDocumentReference:
    def __init__(self):
        self.set_calls = []  # (document_data, merge)

    async def set(self, document_data, merge=False):
        self.set_calls.append((document_data, merge))


class FakeCollection:
    def __init__(self):
        self._interval_ref = FakeSubDocumentReference()

    def document(self, doc_id):
        return self._interval_ref


class FakeDocumentReference:
    def __init__(self, existing_snapshot):
        self._existing_snapshot = existing_snapshot
        self.set_calls = []  # (document_data, merge)
        self._collection = FakeCollection()

    async def get(self, transaction=None):
        return self._existing_snapshot

    async def set(self, document_data, merge=False):
        self.set_calls.append((document_data, merge))

    def collection(self, name):
        return self._collection


class FakeFirestoreClient:
    def __init__(self, sleep_ref):
        self._sleep_ref = sleep_ref

    def collection(self, name):
        return self

    def document(self, doc_id):
        return self._sleep_ref


def _make_interval():
    return SnooCompletedSession(
        session_id="sess-1",
        start=datetime(2026, 7, 1, 20, 0, tzinfo=UTC),
        end=datetime(2026, 7, 1, 20, 30, tzinfo=UTC),
        total_seconds=1800.0,
        notes="Total: 30m",
    )


def test_write_sleep_interval_writes_interval_doc_and_lastsleep():
    interval = _make_interval()
    sleep_ref = FakeDocumentReference(FakeSnapshot(exists=False))
    client = FakeFirestoreClient(sleep_ref)
    hb = AsyncMock()
    hb._get_firestore_client.return_value = client
    hb._get_timezone_offset_minutes.return_value = -240.0

    asyncio.run(write_sleep_interval(hb, "child-1", interval))

    # Interval doc written unconditionally via plain .set() (no transaction).
    assert len(sleep_ref._collection._interval_ref.set_calls) == 1
    payload, merge = sleep_ref._collection._interval_ref.set_calls[0]
    assert merge is False
    assert payload["start"] == interval.start.timestamp()

    # prefs.lastSleep written because stored value was absent (newer than 0).
    assert len(sleep_ref.set_calls) == 1
    prefs_payload, prefs_merge = sleep_ref.set_calls[0]
    assert prefs_merge is True  # merge so parent doc is created if missing
    assert "lastSleep" in prefs_payload["prefs"]


def test_write_sleep_interval_skips_lastsleep_update_when_not_newer():
    interval = _make_interval()
    existing = FakeSnapshot(
        exists=True,
        data={"prefs": {"lastSleep": {"start": interval.start.timestamp() + 3600}}},
    )
    sleep_ref = FakeDocumentReference(existing)
    client = FakeFirestoreClient(sleep_ref)
    hb = AsyncMock()
    hb._get_firestore_client.return_value = client
    hb._get_timezone_offset_minutes.return_value = -240.0

    asyncio.run(write_sleep_interval(hb, "child-1", interval))

    # Interval doc still written...
    assert len(sleep_ref._collection._interval_ref.set_calls) == 1
    # ...but prefs.lastSleep is untouched because the stored value is newer.
    assert sleep_ref.set_calls == []
