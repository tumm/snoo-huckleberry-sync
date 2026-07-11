"""Regression tests for write_sleep_interval's Firestore transaction usage.

Fakes stand in for google-cloud-firestore's real AsyncTransaction (which
needs a live/emulated backend for _begin/_commit) - async_transactional is
patched to a no-op passthrough so the decorated function runs directly
against the fakes below.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from sync.huckleberry_sink import write_sleep_interval
from sync.models import SnooCompletedSession


class FakeSnapshot:
    def __init__(self, exists, data=None):
        self.exists = exists
        self._data = data or {}

    def to_dict(self):
        return self._data


class FakeSubDocumentReference:
    async def set(self, document_data, merge=False):
        raise AssertionError(
            "interval doc .set() must go through txn.set(), not a direct call "
            "- both writes must be in the same transaction"
        )


class FakeCollection:
    def document(self, doc_id):
        return FakeSubDocumentReference()


class FakeDocumentReference:
    def __init__(self, existing_snapshot):
        self._existing_snapshot = existing_snapshot
        self.direct_set_calls = []  # any call NOT routed through a transaction

    async def get(self, transaction=None):
        return self._existing_snapshot

    async def set(self, document_data, merge=False, transaction=None):
        # A real AsyncDocumentReference.set() has NO `transaction` kwarg at
        # all - this fake accepts it just so a regression shows up as an
        # assertion failure below (direct_set_calls non-empty) instead of a
        # TypeError, which would be indistinguishable from other bugs.
        self.direct_set_calls.append((document_data, merge, transaction))

    def collection(self, name):
        return FakeCollection()


class FakeTransaction:
    def __init__(self):
        self.set_calls = []  # (reference, document_data, merge)

    def set(self, reference, document_data, merge=False):
        self.set_calls.append((reference, document_data, merge))


class FakeFirestoreClient:
    def __init__(self, sleep_ref):
        self._sleep_ref = sleep_ref
        self.transaction_instances = []

    def collection(self, name):
        return self

    def document(self, doc_id):
        return self._sleep_ref

    def transaction(self):
        txn = FakeTransaction()
        self.transaction_instances.append(txn)
        return txn


def _make_interval():
    return SnooCompletedSession(
        session_id="sess-1",
        start=datetime(2026, 7, 1, 20, 0, tzinfo=UTC),
        end=datetime(2026, 7, 1, 20, 30, tzinfo=UTC),
        total_seconds=1800.0,
        notes="Total: 30m",
    )


def test_write_sleep_interval_writes_both_docs_via_the_same_transaction():
    interval = _make_interval()
    sleep_ref = FakeDocumentReference(FakeSnapshot(exists=False))
    client = FakeFirestoreClient(sleep_ref)
    hb = AsyncMock()
    hb._get_firestore_client.return_value = client
    hb._get_timezone_offset_minutes.return_value = -240.0

    with patch("sync.huckleberry_sink.async_transactional", lambda f: f):
        asyncio.run(write_sleep_interval(hb, "child-1", interval))

    # Exactly one transaction was used for both writes.
    assert len(client.transaction_instances) == 1
    txn = client.transaction_instances[0]
    assert len(txn.set_calls) == 2  # interval doc + prefs.lastSleep

    # Neither write bypassed the transaction via a bare .set(transaction=...)
    # call (the exact bug this test guards against).
    assert sleep_ref.direct_set_calls == []

    references = [call[0] for call in txn.set_calls]
    assert sleep_ref in references


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

    with patch("sync.huckleberry_sink.async_transactional", lambda f: f):
        asyncio.run(write_sleep_interval(hb, "child-1", interval))

    txn = client.transaction_instances[0]
    # Only the interval doc write happens - prefs.lastSleep is untouched
    # because the stored value is already newer.
    assert len(txn.set_calls) == 1
    assert sleep_ref.direct_set_calls == []
