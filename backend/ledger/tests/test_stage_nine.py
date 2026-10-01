"""Stage 9 — the ledger's half (doc 24 §3 step 3, site 4 + enum parity).

Hermetic (FakeConnection, no live cluster). Drives `reconcile_settled_payments` through the
real post-batch path so the DISCREPANT → exception write emerges from the real stamp, not a
monkeypatch (defect 2026-09-01 `unreachable-control`). Reuses the stage-8 fixtures so the
amount units (majors on payments/paymentExecutions/settlementPositions, minors on
ledgerEvents legs) stay faithful — the same fidelity trap as stage 6/8.
"""

from __future__ import annotations

import json
import pathlib

from services.exceptions_service import (
    ACTION_RECHECK,
    CATEGORY_ORPHANED_SETTLEMENT,
    CATEGORY_RECONCILIATION_MISSING,
    SUBJECT_STATEMENT_LINE,
    CATEGORY_DUPLICATE_SIGNAL,
    CATEGORY_RECONCILIATION_DISCREPANCY,
    CATEGORY_SETTLEMENT_DELAYED,
    CATEGORY_SETTLEMENT_RETURNED,
    CATEGORY_SETTLEMENT_UNMATCHED,
    CATEGORY_UTA,
    SEVERITY_ACTION_REQUIRED,
    SEVERITY_INFORMATIONAL,
    SERVICE_LEDGER,
    SERVICE_TRANSACTIONS,
    SOURCE_STAGE_RECONCILE,
    SOURCE_STAGE_SETTLE,
    SOURCE_STAGE_VALIDATE,
    STATUS_DISMISSED,
    STATUS_OPEN,
    STATUS_RESOLVED,
)
from services.reconciliation_service import reconcile_settled_payments
from tests._fakedb import FakeConnection
from tests.test_stage_eight import (
    _EVT_SET,
    _JNL,
    _PAY,
    _execution,
    _payment_with_recon,
    _position,
    _principal_event,
    _settlement_event,
    _subledger,
)

_STUB = (
    pathlib.Path(__file__).resolve().parent.parent / "data" / "exceptions_schema.json"
)


def _stub_schema() -> dict:
    return json.loads(_STUB.read_text())["validator"]["$jsonSchema"]


def _exceptions(c):
    return c.get_collection("db", "exceptions").docs


def _seed_discrepant(c):
    """A settled wire whose rail execution amount (24000) != payment amount (25000) —
    leg 1 MISMATCH → overall DISCREPANT. Same setup as the stage-8 discrepant test."""
    c.seed("payments", [_payment_with_recon(rail="WIRE", state="SETTLED")])
    c.seed("paymentExecutions", [_execution(amount=24000.0)])  # leg 1 mismatch
    c.seed("settlementPositions", [_position()])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"), _settlement_event()])
    c.seed("subLedgerEntries", [_subledger(_EVT_SET)])


def test_a_discrepant_stamp_inserts_one_reconciliation_discrepancy_exception():
    c = FakeConnection()
    _seed_discrepant(c)

    result = reconcile_settled_payments(c, "db")
    assert result["discrepant"] == 1

    excs = _exceptions(c)
    assert len(excs) == 1
    e = excs[0]
    assert e["category"] == CATEGORY_RECONCILIATION_DISCREPANCY
    assert e["status"] == STATUS_OPEN
    assert e["severity"] == SEVERITY_ACTION_REQUIRED
    assert e["paymentId"] == _PAY
    assert e["source"] == {"stage": SOURCE_STAGE_RECONCILE, "service": SERVICE_LEDGER}
    assert e["sourceSystem"] == SERVICE_LEDGER
    assert e["resolution"] is None
    assert e["agent"] is None
    assert e["exceptionId"].startswith("EXC-")
    # leg 1 mismatch: expected 25000.00, actual 24000.00, discrepancy 1000.00 (majors)
    assert e["detail"]["expectedAmount"] == 25000.0
    assert e["detail"]["actualAmount"] == 24000.0
    assert e["detail"]["discrepancyAmount"] == 1000.0


def test_a_second_reconciliation_pass_does_not_double_the_exception():
    """Site 4 re-checks DISCREPANT payments every batch pass (a discrepancy can resolve).
    The exception write dedupes on (paymentId, category, OPEN) — a second pass must not
    double-insert (B3)."""
    c = FakeConnection()
    _seed_discrepant(c)

    reconcile_settled_payments(c, "db")
    reconcile_settled_payments(c, "db")

    assert len(_exceptions(c)) == 1


def test_a_reconciled_payment_creates_no_exception():
    """Regression — a RECONCILED payment stamps no exception (doc 24 §4 Regression)."""
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="INTERNAL", state="POSTED")])
    c.seed("ledgerEvents", [_principal_event()])

    result = reconcile_settled_payments(c, "db")
    assert result["reconciled"] == 1
    assert _exceptions(c) == []


# --- enum parity: the ledger's constants equal the authored stub (B3 mirror) --


def test_the_ledger_code_enum_constants_match_the_authored_stub():
    p = _stub_schema()["properties"]
    assert {
        CATEGORY_SETTLEMENT_UNMATCHED, CATEGORY_SETTLEMENT_RETURNED,
        CATEGORY_SETTLEMENT_DELAYED, CATEGORY_RECONCILIATION_DISCREPANCY,
        CATEGORY_RECONCILIATION_MISSING, CATEGORY_ORPHANED_SETTLEMENT,
        CATEGORY_DUPLICATE_SIGNAL, CATEGORY_UTA,
    } == set(p["category"]["enum"])
    assert ACTION_RECHECK in p["resolution"]["properties"]["action"]["enum"]
    assert {SUBJECT_STATEMENT_LINE} == set(p["subjectRef"]["properties"]["kind"]["enum"])
    assert {STATUS_OPEN, STATUS_RESOLVED, STATUS_DISMISSED} == set(p["status"]["enum"])
    assert {SEVERITY_ACTION_REQUIRED, SEVERITY_INFORMATIONAL} == set(p["severity"]["enum"])
    assert {
        SOURCE_STAGE_VALIDATE, SOURCE_STAGE_SETTLE, SOURCE_STAGE_RECONCILE,
    } == set(p["source"]["properties"]["stage"]["enum"])
    assert {SERVICE_TRANSACTIONS, SERVICE_LEDGER} == set(p["source"]["properties"]["service"]["enum"])


def test_f6_ledger_record_exception_rejects_a_non_outgoing_category():
    import pytest
    from services.exceptions_service import record_exception

    class _Never:
        def find_one(self, *a, **k):
            raise AssertionError("guard must run before any read")

    with pytest.raises(ValueError, match="not an outgoing category"):
        record_exception(_Never(), "PAY-1", "UTA", None, {"stage": "8 reconcile", "service": "ledger-service"})
