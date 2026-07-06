"""Unit tests for the realtime posting worker's per-change handler.

Hermetic: mocked connection/post_realtime_event, no DB.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pymongo.errors import DuplicateKeyError

import workers.realtime_posting_worker as realtime_posting_worker
from workers.realtime_posting_worker import process_ledger_event_update


def _event(event_id: str = "LE-x", posting_mode: str = "REALTIME") -> dict:
    return {"eventId": event_id, "postingMode": {"type": posting_mode}}


def test_realtime_event_dispatches_to_post_realtime_event(monkeypatch):
    called = MagicMock(return_value=True)
    monkeypatch.setattr(realtime_posting_worker, "post_realtime_event", called)
    connection = MagicMock()

    process_ledger_event_update(_event(), connection=connection, db_name="test_db", coa=None)

    called.assert_called_once_with("LE-x", connection, "test_db", coa=None)


def test_near_realtime_event_dispatches_too(monkeypatch):
    called = MagicMock(return_value=True)
    monkeypatch.setattr(realtime_posting_worker, "post_realtime_event", called)

    process_ledger_event_update(
        _event(posting_mode="NEAR_REALTIME"), connection=MagicMock(), db_name="test_db", coa=None
    )

    called.assert_called_once()


def test_batch_event_is_a_noop(monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(realtime_posting_worker, "post_realtime_event", called)

    process_ledger_event_update(_event(posting_mode="BATCH"), connection=MagicMock(), db_name="test_db", coa=None)

    called.assert_not_called()


def test_missing_event_id_is_a_noop(monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(realtime_posting_worker, "post_realtime_event", called)

    process_ledger_event_update({"postingMode": {"type": "REALTIME"}}, connection=MagicMock(), db_name="test_db", coa=None)

    called.assert_not_called()


def test_duplicate_key_error_is_swallowed(monkeypatch):
    monkeypatch.setattr(
        realtime_posting_worker,
        "post_realtime_event",
        MagicMock(side_effect=DuplicateKeyError("dup")),
    )

    # Must not raise.
    process_ledger_event_update(_event(), connection=MagicMock(), db_name="test_db", coa=None)


def test_other_exception_is_caught_and_logged(monkeypatch):
    monkeypatch.setattr(
        realtime_posting_worker,
        "post_realtime_event",
        MagicMock(side_effect=RuntimeError("boom")),
    )

    # Must not raise — a Stage-③ failure leaves the event visibly PENDING,
    # it must not crash the change-stream worker.
    process_ledger_event_update(_event(), connection=MagicMock(), db_name="test_db", coa=None)
