"""Reconciliation plan A2 — statement matching (decision D1a: match on reference, record the
amount as found).

Fixture fidelity: `_line` copies the keys of the transactions service's
`camt053.entry_for` / `orphan_entry` (the production writer), and `_statement` the keys of
`inbound_documents.statement_doc` that the matcher reads. The ledger cannot import that code
(separate service, separate venv), so the shape is mirrored here — the same discipline as
the exceptions stub's twin.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timezone

from services.reconciliation_service import LEG_MATCH, LEG_MISMATCH, LEG_PENDING, compute_reconciliation
from services.statement_matching import match_statements
from tests._fakedb import FakeConnection

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _unmatched() -> dict:
    return {"status": "UNMATCHED", "matchedPaymentId": None, "matchedBy": None, "at": None}


def _line(line_no: int, reference: str, amount: float, *, charges=None,
          simulated_pid=None, simulated_outcome="CLEAN") -> dict:
    return {
        "lineNo": line_no, "reference": reference, "amount": amount, "currency": "USD",
        "creditDebit": "DBIT", "charges": charges,
        "simulatedPaymentId": simulated_pid, "simulatedOutcome": simulated_outcome,
        "recon": _unmatched(),
    }


def _statement(entries: list[dict], account_code: str = "1111") -> dict:
    return {
        "_id": "oid-stmt-1",
        "paymentMessageId": "PM-STMT0001", "paymentId": None,
        "direction": "INBOUND", "purpose": "ACCOUNT_STATEMENT",
        "messageFormat": "camt.053.001.08",
        "statement": {"accountCode": account_code, "currency": "USD", "sequence": 1},
        "entries": entries,
    }


def _position(pid: str, amount: float = 25000.0, account_code: str = "1111") -> dict:
    # As `settle._write_settlement_position` writes it after A2: no actual amount at settle.
    return {
        "_id": f"oid-sp-{pid}",
        "settlementPositionId": f"SP-{pid}", "paymentId": pid, "rail": "WIRE",
        "settlementAccountCode": account_code, "clearingAccountCode": "1131",
        "expectedAmount": amount, "expectedCurrency": "USD",
        "actualAmount": None, "actualCurrency": None,
        "grossAmount": amount, "currency": "USD",
        "outcome": "MATCHED", "settlementStatus": "SETTLED",
        "createdAt": _NOW,
    }


def _seed(c: FakeConnection, entries, positions, account_code="1111"):
    c.seed("paymentMessages", [_statement(entries, account_code)])
    c.seed("settlementPositions", positions)


def _recon(c, line_no):
    stmt = c.get_collection("db", "paymentMessages").find_one({"purpose": "ACCOUNT_STATEMENT"})
    return next(e for e in stmt["entries"] if e["lineNo"] == line_no)["recon"]


def _pos(c, pid):
    return c.get_collection("db", "settlementPositions").find_one({"paymentId": pid})


def test_a_clean_line_auto_matches_on_the_exact_key():
    c = FakeConnection()
    _seed(c, [_line(1, "PAY-A", 25000.0)], [_position("PAY-A")])

    result = match_statements(c, "db", now=_NOW)

    assert result == {"lines": 1, "matched": 1, "unmatched": 0}
    assert _recon(c, 1) == {"status": "AUTO_MATCHED", "matchedPaymentId": "PAY-A",
                            "matchedBy": "EXACT_KEY", "at": _NOW}
    pos = _pos(c, "PAY-A")
    assert pos["actualAmount"] == 25000.0
    assert pos["actualBookedAt"] == _NOW
    assert pos["sourceMessageRef"] == "PM-STMT0001"


def test_a_fee_deducted_line_matches_by_reference_and_records_the_short_amount():
    """D1a — the amount is recorded as found; leg 2 reports the delta, not the matcher."""
    c = FakeConnection()
    _seed(c, [_line(1, "PAY-A", 24975.0, charges=25.0)], [_position("PAY-A")])

    match_statements(c, "db", now=_NOW)

    assert _recon(c, 1)["matchedBy"] == "REFERENCE_ONLY"
    assert _pos(c, "PAY-A")["actualAmount"] == 24975.0


def test_an_altered_reference_and_an_orphan_stay_unmatched():
    c = FakeConnection()
    _seed(c, [_line(1, "A1B2C3D4/LEAFYBK", 25000.0, simulated_pid="PAY-A",
                    simulated_outcome="REFERENCE_ALTERED"),
              _line(2, "ORPH-11959625", 1250.0, simulated_outcome="ORPHAN")],
          [_position("PAY-A")])

    result = match_statements(c, "db", now=_NOW)

    assert result == {"lines": 2, "matched": 0, "unmatched": 2}
    assert _recon(c, 1)["status"] == "UNMATCHED"
    assert _recon(c, 2)["status"] == "UNMATCHED"
    assert _pos(c, "PAY-A")["actualAmount"] is None


def test_a_line_on_the_wrong_settlement_account_does_not_match():
    c = FakeConnection()
    _seed(c, [_line(1, "PAY-A", 25000.0)], [_position("PAY-A", account_code="1121")],
          account_code="1111")

    match_statements(c, "db", now=_NOW)

    assert _recon(c, 1)["status"] == "UNMATCHED"
    assert _pos(c, "PAY-A")["actualAmount"] is None


def test_matching_is_idempotent():
    c = FakeConnection()
    _seed(c, [_line(1, "PAY-A", 25000.0)], [_position("PAY-A")])
    match_statements(c, "db", now=_NOW)

    again = match_statements(c, "db", now=datetime(2026, 10, 1, tzinfo=timezone.utc))

    assert again == {"lines": 0, "matched": 0, "unmatched": 0}
    assert _recon(c, 1)["at"] == _NOW


def test_a_position_already_holding_an_actual_amount_is_not_overwritten():
    """Pre-A2 positions carry a self-written actual; history is left as it was."""
    c = FakeConnection()
    pos = {**_position("PAY-A"), "actualAmount": 25000.0}
    _seed(c, [_line(1, "PAY-A", 24975.0, charges=25.0)], [pos])

    match_statements(c, "db", now=_NOW)

    assert _recon(c, 1)["status"] == "UNMATCHED"
    assert _pos(c, "PAY-A")["actualAmount"] == 25000.0


def test_the_matcher_never_reads_simulator_truth():
    src = pathlib.Path(__file__).parent.parent.joinpath("services", "statement_matching.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    code = code.split('"""', 2)[-1]  # drop the module docstring, which names them
    assert "simulatedPaymentId" not in code
    assert "simulatedOutcome" not in code


# --- the seam: matcher → leg 2 -------------------------------------------------

_MIN = 2_500_000


def _wire_fixture(c, line_amount: float):
    c.seed("payments", [{"paymentId": "PAY-A", "rail": "WIRE", "amount": 25000.0,
                         "currency": "USD", "status": "SETTLED",
                         "lifecycle": {"currentState": "SETTLED"},
                         "refs": {"journalEntryId": "JE-1"}}])
    c.seed("paymentExecutions", [{"paymentId": "PAY-A", "attempt": 1, "amount": 25000.0,
                                  "currency": "USD", "status": "ACKNOWLEDGED",
                                  "railStatus": {"code": "ACSC"}}])
    c.seed("ledgerEvents", [
        {"eventId": "EVT-P", "idempotencyKey": "PAY-A", "postingStatus": "POSTED",
         "debitLeg": {"glAccountCode": "2111", "amount": _MIN, "currency": "USD"},
         "creditLeg": {"glAccountCode": "1131", "amount": _MIN, "currency": "USD"}},
        {"eventId": "EVT-S", "idempotencyKey": "PAY-A-SETTLEMENT", "postingStatus": "POSTED",
         "debitLeg": {"glAccountCode": "1131", "amount": _MIN, "currency": "USD"},
         "creditLeg": {"glAccountCode": "1111", "amount": _MIN, "currency": "USD"}},
    ])
    c.seed("subLedgerEntries", [{"sourceReference": {"sourceId": "EVT-S"},
                                 "journalEntryId": "JE-1", "controlAccountCode": "1131"}])
    _seed(c, [_line(1, "PAY-A", line_amount)], [_position("PAY-A")])


def test_leg2_waits_for_the_statement_then_matches_a_clean_line():
    c = FakeConnection()
    _wire_fixture(c, 25000.0)

    assert compute_reconciliation("PAY-A", c, "db").legs[1].result == LEG_PENDING
    match_statements(c, "db", now=_NOW)
    assert compute_reconciliation("PAY-A", c, "db").legs[1].result == LEG_MATCH


def test_leg2_flags_the_fee_deducted_short_pay_after_matching():
    c = FakeConnection()
    _wire_fixture(c, 24975.0)

    match_statements(c, "db", now=_NOW)
    leg = compute_reconciliation("PAY-A", c, "db").legs[1]

    assert leg.result == LEG_MISMATCH
    assert leg.left_amount - leg.right_amount == 2500


def test_leg2_does_not_wait_on_a_statement_for_an_inbound_payment():
    """Inbound positions carry their actual at arrival and never appear on a statement."""
    c = FakeConnection()
    _wire_fixture(c, 25000.0)
    payments = c.get_collection("db", "payments")
    payments.update_one({"paymentId": "PAY-A"}, {"$set": {"direction": "INBOUND"}})
    positions = c.get_collection("db", "settlementPositions")
    positions.update_one({"paymentId": "PAY-A"}, {"$set": {"actualAmount": 25000.0}})

    assert compute_reconciliation("PAY-A", c, "db").legs[1].result != LEG_PENDING
