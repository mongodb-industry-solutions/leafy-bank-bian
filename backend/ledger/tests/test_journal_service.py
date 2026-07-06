"""Unit tests for the journal service pure functions.

Hermetic: inline aggregation-row + CoA fixtures, no DB.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from bson import Int64

import services.journal_service as journal_service
from services.journal_service import (
    assert_balanced_journal,
    build_journal_entry,
    post_realtime_event,
    run_batch,
    sweep_stale_realtime_postings,
)
from shared.coa_cache import ChartOfAccounts


def _coa() -> ChartOfAccounts:
    # Two control accounts (non-posting) used by the journal builder.
    return ChartOfAccounts([
        {"accountCode": "2110", "accountName": "Current Accounts - Control", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": "2100"},
        {"accountCode": "2120", "accountName": "Savings Accounts - Control", "isPostingAccount": False, "status": "ACTIVE", "parentAccountCode": "2100"},
    ])


def _agg(
    code: str,
    side: str,
    amount: int,
    count: int = 1,
    sub_ids: list[str] | None = None,
    event_ids: list[str] | None = None,
) -> dict:
    """Build one aggregation row in the shape produced by run_batch's $group pipeline."""
    return {
        "_id": {"controlAccountCode": code, "side": side},
        "amount": amount,
        "currency": "USD",
        "count": count,
        "subLedgerIds": sub_ids or [f"SL-{code}-{side}-001"],
        "eventIds": event_ids or [f"LE-{code}-{side}-001"],
    }


_BATCH_ID = "BATCH-20260623T1415"
_PERIOD = "2026-06"


def _balanced_agg_rows(amount: int = 25000) -> list[dict]:
    return [_agg("2110", "DEBIT", amount), _agg("2120", "CREDIT", amount)]


# --- assert_balanced_journal --------------------------------------------------

def test_balanced_does_not_raise():
    assert_balanced_journal([{"side": "DEBIT", "amount": 100}, {"side": "CREDIT", "amount": 100}])


def test_unbalanced_raises():
    with pytest.raises(ValueError, match="unbalanced"):
        assert_balanced_journal([{"side": "DEBIT", "amount": 100}, {"side": "CREDIT", "amount": 99}])


# --- build_journal_entry: return type -----------------------------------------

def test_returns_three_tuple():
    result = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert isinstance(result, tuple) and len(result) == 3


def test_journal_id_has_correct_prefix():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert journal["journalId"].startswith("JNL-")


# --- idempotency key ----------------------------------------------------------

def test_idempotency_key_encodes_batch_and_period():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert journal["idempotencyKey"] == f"JOURNAL-{_BATCH_ID}-{_PERIOD}"


# --- journal structure --------------------------------------------------------

def test_journal_status_is_posted():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert journal["status"] == "POSTED"


def test_journal_type_is_system():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert journal["journalType"] == "SYSTEM"


def test_journal_period_code_carried_through():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert journal["periodCode"] == _PERIOD


def test_journal_has_one_line_per_agg_row():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert len(journal["entries"]) == 2


def test_journal_lines_debit_first_then_account_code_asc():
    # Same account code on both sides → DEBIT before CREDIT.
    rows = [_agg("2110", "CREDIT", 25000), _agg("2110", "DEBIT", 25000)]
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, rows)
    sides = [e["side"] for e in journal["entries"]]
    assert sides == ["DEBIT", "CREDIT"]


def test_journal_lines_have_sequential_line_numbers():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    numbers = [line["lineNumber"] for line in journal["entries"]]
    assert numbers == list(range(1, len(numbers) + 1))


def test_journal_lines_carry_account_code():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    codes = {line["accountCode"] for line in journal["entries"]}
    assert codes == {"2110", "2120"}


def test_journal_lines_have_line_description_starting_with_sum():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    for line in journal["entries"]:
        assert line["lineDescription"].startswith("Sum of ")


def test_journal_lines_line_description_includes_count():
    rows = [_agg("2110", "DEBIT", 75000, count=3), _agg("2120", "CREDIT", 75000, count=3)]
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, rows)
    debit_line = next(e for e in journal["entries"] if e["side"] == "DEBIT")
    assert "3" in debit_line["lineDescription"]


def test_journal_lines_have_no_sub_ledger_ref():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    for line in journal["entries"]:
        assert "subLedgerRef" not in line


def test_journal_lines_have_no_entity_reference():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    for line in journal["entries"]:
        assert "entityReference" not in line


def test_journal_doc_has_no_group_id():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert "groupId" not in journal


# --- money fields are BSON long -----------------------------------------------

def test_line_amount_is_bson_int64():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows(25000))
    for line in journal["entries"]:
        assert isinstance(line["amount"], Int64)


def test_line_functional_amount_is_bson_int64():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows(25000))
    for line in journal["entries"]:
        assert isinstance(line["functionalAmount"], Int64)


def test_total_amount_is_bson_int64():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows(25000))
    assert isinstance(journal["totalAmount"], Int64)


def test_total_amount_equals_debit_sum():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows(amount=25000))
    assert int(journal["totalAmount"]) == 25000


# --- date fields are strings --------------------------------------------------

def test_value_date_is_string():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert isinstance(journal["valueDate"], str)


def test_posting_date_is_string():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert isinstance(journal["postingDate"], str)


def test_created_at_is_string():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert isinstance(journal["createdAt"], str)


def test_updated_at_is_string():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert isinstance(journal["updatedAt"], str)


# --- SoD / createdBy ----------------------------------------------------------

def test_created_by_is_set():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert journal["createdBy"] == "GL_BATCH_PIPELINE"


def test_approved_by_absent():
    # SoD: validator enforces createdBy != approvedBy; leaving approvedBy absent satisfies $ne.
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert "approvedBy" not in journal


# --- sourceReference ----------------------------------------------------------

def test_source_reference_has_required_fields():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    sr = journal["sourceReference"]
    assert sr["sourceSystem"] == "GL_BATCH_PIPELINE"
    assert sr["sourceType"] == "BATCH_POSTING"
    assert sr["sourceId"] == _BATCH_ID


def test_source_reference_has_no_legacy_ledger_event_type():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert journal["sourceReference"]["sourceType"] != "LEDGER_EVENT"


# --- sub_ids / event_ids accumulation -----------------------------------------

def test_sub_ids_aggregated_from_all_rows():
    rows = [
        _agg("2110", "DEBIT", 25000, sub_ids=["SL-001", "SL-002"]),
        _agg("2120", "CREDIT", 25000, sub_ids=["SL-003"]),
    ]
    _, sub_ids, _ = build_journal_entry(_BATCH_ID, _PERIOD, rows)
    assert set(sub_ids) == {"SL-001", "SL-002", "SL-003"}


def test_event_ids_deduplicated():
    rows = [
        _agg("2110", "DEBIT", 25000, event_ids=["LE-001", "LE-002"]),
        _agg("2120", "CREDIT", 25000, event_ids=["LE-001", "LE-003"]),
    ]
    _, _, event_ids = build_journal_entry(_BATCH_ID, _PERIOD, rows)
    # event_ids may have duplicates (plain extend); distinct count checked via sourceReference.txnCount
    assert "LE-001" in event_ids
    assert "LE-002" in event_ids
    assert "LE-003" in event_ids


def test_txn_count_is_distinct_event_count():
    rows = [
        _agg("2110", "DEBIT", 25000, event_ids=["LE-001", "LE-002"]),
        _agg("2120", "CREDIT", 25000, event_ids=["LE-001", "LE-003"]),
    ]
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, rows)
    assert journal["sourceReference"]["txnCount"] == 3  # LE-001, LE-002, LE-003


# --- coa validation -----------------------------------------------------------

def test_build_with_coa_validates_control_accounts():
    # coa.require_active_control_account must not raise for the two control codes.
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows(), coa=_coa())
    assert journal["entries"][0]["accountName"] == "Current Accounts - Control"


def test_build_with_coa_rejects_unknown_control_code():
    rows = [_agg("9999", "DEBIT", 100), _agg("9998", "CREDIT", 100)]
    with pytest.raises(ValueError, match="not found"):
        build_journal_entry(_BATCH_ID, _PERIOD, rows, coa=_coa())


# --- balance guard ------------------------------------------------------------

def test_build_raises_on_unbalanced_agg_input():
    rows = [_agg("2110", "DEBIT", 25001), _agg("2120", "CREDIT", 25000)]
    with pytest.raises(ValueError, match="unbalanced"):
        build_journal_entry(_BATCH_ID, _PERIOD, rows)


# --- same control account on both sides (never netted) ------------------------

def test_same_control_account_both_sides_produces_two_lines():
    # Internal transfer: same control account on DR and CR → two lines, balanced.
    rows = [_agg("2110", "DEBIT", 25000), _agg("2110", "CREDIT", 25000)]
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, rows)
    assert len(journal["entries"]) == 2
    sides = [e["side"] for e in journal["entries"]]
    assert sides == ["DEBIT", "CREDIT"]


# --- build_journal_entry: realtime overrides ----------------------------------

def test_realtime_overrides_flow_into_journal():
    journal, _, _ = build_journal_entry(
        "RT-LE-abc", _PERIOD, _balanced_agg_rows(),
        idempotency_key="JOURNAL-RT-LE-abc",
        source_type="REALTIME_POSTING",
        source_id="LE-abc",
    )
    assert journal["idempotencyKey"] == "JOURNAL-RT-LE-abc"
    assert journal["sourceReference"]["sourceType"] == "REALTIME_POSTING"
    assert journal["sourceReference"]["sourceId"] == "LE-abc"
    assert journal["journalType"] == "SYSTEM"  # validator only allows SYSTEM/MANUAL/REVERSAL


def test_overrides_default_to_batch_values():
    journal, _, _ = build_journal_entry(_BATCH_ID, _PERIOD, _balanced_agg_rows())
    assert journal["idempotencyKey"] == f"JOURNAL-{_BATCH_ID}-{_PERIOD}"
    assert journal["sourceReference"]["sourceType"] == "BATCH_POSTING"
    assert journal["sourceReference"]["sourceId"] == _BATCH_ID


# --- post_realtime_event (mocked DB) ------------------------------------------

def _sl(code: str, side: str, amount: int, event_id: str, sub_id: str, period: str = _PERIOD) -> dict:
    """One subLedgerEntry doc as post_realtime_event reads it."""
    return {
        "controlAccountCode": code,
        "side": side,
        "amount": amount,
        "currency": "USD",
        "subLedgerId": sub_id,
        "periodCode": period,
        "status": "POSTED",
        "journalEntryId": "",
        "sourceReference": {"sourceId": event_id},
    }


def _conn_returning(sl_rows: list[dict]):
    coll = MagicMock()
    coll.find.return_value = iter(sl_rows)
    conn = MagicMock()
    conn.get_collection.return_value = coll
    return conn


def test_post_realtime_event_writes_one_journal(monkeypatch):
    captured = {}

    def _fake_write(journal, sub_ids, event_ids, connection, db_name):
        captured["journal"] = journal
        captured["sub_ids"] = sub_ids
        return True

    monkeypatch.setattr(journal_service, "write_journal", _fake_write)
    rows = [
        _sl("2110", "DEBIT", 25000, "LE-x", "SL-1"),
        _sl("2120", "CREDIT", 25000, "LE-x", "SL-2"),
    ]
    result = post_realtime_event("LE-x", _conn_returning(rows), "test_db", coa=_coa())

    assert result is True
    assert captured["journal"]["idempotencyKey"] == "JOURNAL-RT-LE-x"
    assert captured["journal"]["sourceReference"]["sourceType"] == "REALTIME_POSTING"
    assert len(captured["journal"]["entries"]) == 2
    assert set(captured["sub_ids"]) == {"SL-1", "SL-2"}


def test_post_realtime_event_skips_when_no_rows(monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(journal_service, "write_journal", called)
    result = post_realtime_event("LE-none", _conn_returning([]), "test_db")
    assert result is False
    called.assert_not_called()


def test_post_realtime_event_skips_incomplete_pair(monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(journal_service, "write_journal", called)
    rows = [_sl("2110", "DEBIT", 25000, "LE-x", "SL-1")]  # only one leg
    result = post_realtime_event("LE-x", _conn_returning(rows), "test_db")
    assert result is False
    called.assert_not_called()


# --- run_batch: postingMode routing exclusion ---------------------------------

def _dispatch_conn(subledger_coll, ledger_events_coll):
    conn = MagicMock()

    def _get(_db, name):
        return ledger_events_coll if name == "ledgerEvents" else subledger_coll

    conn.get_collection.side_effect = _get
    return conn


def test_run_batch_excludes_realtime_events(monkeypatch):
    # Completeness aggregation yields one complete pair for a realtime event...
    sl_coll = MagicMock()
    sl_coll.aggregate.return_value = [
        {"_id": "LE-rt", "count": 2, "sides": ["DEBIT", "CREDIT"], "periodCodes": [_PERIOD]},
    ]
    # ...but ledgerEvents.find (postingMode=BATCH) returns nothing → batch posts 0.
    le_coll = MagicMock()
    le_coll.find.return_value = iter([])

    write_called = MagicMock()
    monkeypatch.setattr(journal_service, "write_journal", write_called)

    written = run_batch(_dispatch_conn(sl_coll, le_coll), "test_db")

    assert written == 0
    write_called.assert_not_called()
    # The BATCH-only filter must be applied against ledgerEvents.
    assert le_coll.find.call_args[0][0]["postingMode.type"] == "BATCH"


# --- sweep_stale_realtime_postings ---------------------------------------------

def test_sweep_posts_stale_realtime_event(monkeypatch):
    sl_coll = MagicMock()
    sl_coll.distinct.return_value = ["LE-stale-rt"]
    le_coll = MagicMock()
    le_coll.find.return_value = iter([{"eventId": "LE-stale-rt"}])

    posted = MagicMock(return_value=True)
    monkeypatch.setattr(journal_service, "post_realtime_event", posted)

    count = sweep_stale_realtime_postings(_dispatch_conn(sl_coll, le_coll), "test_db")

    assert count == 1
    posted.assert_called_once()
    # The REALTIME/NEAR_REALTIME filter must be applied against ledgerEvents.
    assert le_coll.find.call_args[0][0]["postingMode.type"]["$in"] == ["REALTIME", "NEAR_REALTIME"]


def test_sweep_excludes_stale_batch_event(monkeypatch):
    sl_coll = MagicMock()
    sl_coll.distinct.return_value = ["LE-stale-batch"]
    le_coll = MagicMock()
    le_coll.find.return_value = iter([])  # BATCH event filtered out by ledgerEvents.find

    posted = MagicMock()
    monkeypatch.setattr(journal_service, "post_realtime_event", posted)

    count = sweep_stale_realtime_postings(_dispatch_conn(sl_coll, le_coll), "test_db")

    assert count == 0
    posted.assert_not_called()


def test_sweep_skips_when_nothing_stale(monkeypatch):
    sl_coll = MagicMock()
    sl_coll.distinct.return_value = []
    le_coll = MagicMock()

    posted = MagicMock()
    monkeypatch.setattr(journal_service, "post_realtime_event", posted)

    count = sweep_stale_realtime_postings(_dispatch_conn(sl_coll, le_coll), "test_db")

    assert count == 0
    posted.assert_not_called()
    le_coll.find.assert_not_called()


def test_sweep_continues_after_one_event_fails(monkeypatch):
    sl_coll = MagicMock()
    sl_coll.distinct.return_value = ["LE-bad", "LE-good"]
    le_coll = MagicMock()
    le_coll.find.return_value = iter([{"eventId": "LE-bad"}, {"eventId": "LE-good"}])

    def _fake_post(event_id, *args, **kwargs):
        if event_id == "LE-bad":
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(journal_service, "post_realtime_event", _fake_post)

    count = sweep_stale_realtime_postings(_dispatch_conn(sl_coll, le_coll), "test_db")

    assert count == 1
