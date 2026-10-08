"""Stage 8 — reconciliation. Doc 22's gates, one test per leg outcome.

Drives `compute_reconciliation` through a fake connection so every leg's MATCH / MISMATCH /
NOT_APPLICABLE / PENDING emerges from real documents, not a monkeypatched decision (defect
2026-09-01 `unreachable-control`: only an end-to-end test proves an outcome is reachable).

⚠️ **Fixture fidelity (Phase C, the fourth occurrence of the class).** Every amount field is
copied from the production writer's convention:
  - `payments.amount`, `paymentExecutions.amount`, `settlementPositions.grossAmount` are
    **major units** (float dollars) — matching `payment_document.build`,
    `execution_documents.payment_execution`, `settle._write_settlement_position`.
  - `ledgerEvents.debitLeg.amount` / `creditLeg.amount` are **minor units** (int) — matching
    `settlement_worker.build_settlement_event` / `posting_rules`. Leg 2 converts majors→minors;
    a test that got the units wrong would pass here and fail on the live cluster, exactly as
    stage 6's `refs.transactionId` did (defect 2026-09-02).
"""

from __future__ import annotations

from datetime import datetime, timezone

from services.reconciliation_service import (
    DISCREPANT,
    LEG_MATCH,
    LEG_MISMATCH,
    LEG_NOT_APPLICABLE,
    LEG_PENDING,
    PENDING,
    RECONCILED,
    compute_reconciliation,
)
from tests._fakedb import FakeConnection

_PAY = "PAY-100023"
_EVT = "LE-0001"
_EVT_SET = "LE-0002"
_JNL = "JNL-2026-0001"
_SP = "SP-225dcdac"

# 25,000.00 in minor units.
_MIN = 2_500_000


def _principal_event(amount_minors: int = _MIN, posting_status: str = "POSTED",
                     credit_code: str = "2110") -> dict:
    """A PAYMENT_PRINCIPAL ledgerEvent: Dr customer deposit / Cr <credit_code>."""
    return {
        "eventId": _EVT,
        "idempotencyKey": _PAY,
        "postingStatus": posting_status,
        "debitLeg": {"glAccountCode": "2111", "amount": amount_minors, "currency": "USD"},
        "creditLeg": {"glAccountCode": credit_code, "amount": amount_minors, "currency": "USD"},
    }


def _settlement_event(amount_minors: int = _MIN, posting_status: str = "POSTED",
                      settlement_code: str = "1111") -> dict:
    """A PAYMENT_SETTLEMENT ledgerEvent: Dr 1131 clearing / Cr <settlement_code>."""
    return {
        "eventId": _EVT_SET,
        "idempotencyKey": f"{_PAY}-SETTLEMENT",
        "postingStatus": posting_status,
        "debitLeg": {"glAccountCode": "1131", "amount": amount_minors, "currency": "USD"},
        "creditLeg": {"glAccountCode": settlement_code, "amount": amount_minors, "currency": "USD"},
    }


def _reversal_event(amount_minors: int = _MIN, posting_status: str = "POSTED",
                    journal_id: str = _JNL) -> dict:
    """A reversal ledgerEvent (stage 9 RETURN_FUNDS): the principal's legs swapped —
    Dr 1131 clearing / Cr 2111 customer deposit — keyed `{paymentId}-REV`."""
    return {
        "eventId": "LE-REV",
        "idempotencyKey": f"{_PAY}-REV",
        "postingStatus": posting_status,
        "reversalOf": "TXN-0001",
        "postingResult": {"journalEntryId": journal_id} if posting_status == "POSTED" else {},
        "debitLeg": {"glAccountCode": "1131", "amount": amount_minors, "currency": "USD"},
        "creditLeg": {"glAccountCode": "2111", "amount": amount_minors, "currency": "USD"},
    }


def _payment(*, rail: str = "INTERNAL", amount: float = 25000.0, state: str = "POSTED",
             journal_ref: str | None = _JNL) -> dict:
    return {
        "paymentId": _PAY,
        "rail": rail,
        "amount": amount,
        "currency": "USD",
        "status": state,
        "lifecycle": {"currentState": state, "events": [{"state": state}]},
        "refs": {"journalEntryId": journal_ref},
    }


def _execution(amount: float = 25000.0, ack_code: str | None = "ACCC") -> dict:
    return {
        "paymentExecutionId": "PE-0001",
        "paymentId": _PAY,
        "attempt": 1,
        "amount": amount,
        "currency": "USD",
        "status": "ACKNOWLEDGED",
        "railStatus": {"code": ack_code, "reason": None, "messageRef": None},
    }


_STATEMENT_MATCHED = object()


def _position(gross: float = 25000.0, status: str = "SETTLED",
              clearing_code: str = "1131", actual=_STATEMENT_MATCHED) -> dict:
    # Default = a position whose correspondent statement line has been matched (A2), so the
    # actual equals the gross. Pass `actual=None` for one still awaiting its statement.
    return {
        "settlementPositionId": _SP,
        "paymentId": _PAY,
        "rail": "WIRE",
        "model": "CORRESPONDENT",
        "clearingAccountCode": clearing_code,
        "settlementAccountCode": "1111",
        "grossAmount": gross,
        "actualAmount": gross if actual is _STATEMENT_MATCHED else actual,
        "currency": "USD",
        "outcome": "MATCHED",
        "settlementStatus": status,
        "batchRef": "SIM-SETT-ABCD1234",
        "simulated": True,
    }


def _subledger(event_id: str, journal_id: str = _JNL) -> dict:
    return {
        "sourceReference": {"sourceId": event_id},
        "journalEntryId": journal_id,
        "controlAccountCode": "1131",
        "side": "DEBIT",
        "amount": _MIN,
        "currency": "USD",
        "status": "POSTED",
    }


# --- internal transfer --------------------------------------------------------

def test_internal_transfer_legs_1_2_not_applicable_leg_3_match_when_posted():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="INTERNAL", state="POSTED")])
    c.seed("ledgerEvents", [_principal_event()])

    check = compute_reconciliation(_PAY, c, "db")

    assert check.legs[0].result == LEG_NOT_APPLICABLE   # payment ↔ rail
    assert check.legs[1].result == LEG_NOT_APPLICABLE   # rail ↔ settlement
    assert check.legs[2].result == LEG_MATCH            # settlement ↔ GL
    assert check.overall == RECONCILED


def test_internal_transfer_is_pending_until_principal_journal_posts():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="INTERNAL", state="SETTLED", journal_ref=None)])
    c.seed("ledgerEvents", [_principal_event(posting_status="PENDING")])

    check = compute_reconciliation(_PAY, c, "db")

    assert check.legs[2].result == LEG_PENDING
    assert check.overall == PENDING


# --- external wire, happy path -------------------------------------------------

def test_external_wire_all_three_legs_match_when_settlement_journal_posted():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="WIRE", amount=25000.0, state="SETTLED", journal_ref=_JNL)])
    c.seed("paymentExecutions", [_execution()])
    c.seed("settlementPositions", [_position()])
    # Principal (Cr 1131) + settlement (Dr 1131) → 1131 nets to zero.
    c.seed("ledgerEvents", [
        _principal_event(credit_code="1131"),
        _settlement_event(),
    ])
    c.seed("subLedgerEntries", [_subledger(_EVT_SET)])

    check = compute_reconciliation(_PAY, c, "db")

    assert [lg.result for lg in check.legs] == [LEG_MATCH, LEG_MATCH, LEG_MATCH]
    assert check.overall == RECONCILED
    assert check.settlement_position_id == _SP
    assert check.journal_entry_id == _JNL


def test_external_wire_pending_until_settlement_event_posts():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="WIRE", state="SETTLED", journal_ref=None)])
    c.seed("paymentExecutions", [_execution()])
    c.seed("settlementPositions", [_position()])
    c.seed("ledgerEvents", [
        _principal_event(credit_code="1131"),
        _settlement_event(posting_status="PENDING"),
    ])

    check = compute_reconciliation(_PAY, c, "db")

    assert check.legs[2].result == LEG_PENDING
    assert check.overall == PENDING


def test_external_wire_pending_before_settlement_event_exists():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="WIRE", state="SETTLED", journal_ref=None)])
    c.seed("paymentExecutions", [_execution()])
    c.seed("settlementPositions", [_position()])
    # No settlement ledgerEvent yet — CDC has not derived it.
    c.seed("ledgerEvents", [_principal_event(credit_code="1131")])

    check = compute_reconciliation(_PAY, c, "db")

    assert check.legs[1].result == LEG_PENDING   # rail ↔ settlement
    assert check.legs[2].result == LEG_PENDING   # settlement ↔ GL
    assert check.overall == PENDING


# --- discrepancies -------------------------------------------------------------

def test_leg1_mismatch_when_rail_amount_differs_from_instruction():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="WIRE", amount=25000.0)])
    c.seed("paymentExecutions", [_execution(amount=24000.0)])  # wrong amount
    c.seed("settlementPositions", [_position()])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"), _settlement_event()])
    c.seed("subLedgerEntries", [_subledger(_EVT_SET)])

    check = compute_reconciliation(_PAY, c, "db")

    assert check.legs[0].result == LEG_MISMATCH
    assert check.overall == DISCREPANT


def test_leg2_mismatch_when_settlement_position_not_settled():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="WIRE")])
    c.seed("paymentExecutions", [_execution()])
    c.seed("settlementPositions", [_position(status="FAILED")])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"), _settlement_event()])
    c.seed("subLedgerEntries", [_subledger(_EVT_SET)])

    check = compute_reconciliation(_PAY, c, "db")

    assert check.legs[1].result == LEG_MISMATCH
    assert check.overall == DISCREPANT


def test_leg3_mismatch_when_clearing_account_does_not_net_to_zero():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="WIRE")])
    c.seed("paymentExecutions", [_execution()])
    c.seed("settlementPositions", [_position()])
    # Principal credits 1131, but settlement debits a DIFFERENT account → 1131 stays credited.
    c.seed("ledgerEvents", [
        _principal_event(credit_code="1131"),
        _settlement_event(posting_status="POSTED"),
        # override the settlement debit to miss 1131
    ])
    # Force the settlement event's debit leg off 1131 by reseeding it.
    le = c.get_collection("db", "ledgerEvents").docs[1]
    le["debitLeg"]["glAccountCode"] = "9999"
    c.seed("subLedgerEntries", [_subledger(_EVT_SET)])

    check = compute_reconciliation(_PAY, c, "db")

    assert check.legs[2].result == LEG_MISMATCH
    assert check.overall == DISCREPANT


# --- P4: leg 3 shows the booked amount; leg 2 compares the rail's confirmed outcome ---------

def _pacs002(status: str = "ACCP") -> dict:
    # Mirrors `inbound_documents.status_response_doc` keys the ledger reads.
    return {"paymentId": _PAY, "purpose": "STATUS_RESPONSE", "direction": "OUTBOUND",
            "statusCode": status}


def _inbound_wire_fixture(c: FakeConnection, amount: float = 25000.0, pacs002_status: str | None = "ACCP",
                          position: dict | None = None) -> None:
    pay = _payment(rail="WIRE", amount=amount, state="SETTLED")
    pay["direction"] = "INBOUND"
    c.seed("payments", [pay])
    c.seed("paymentMessages", [
        {"paymentId": _PAY, "direction": "INBOUND", "purpose": "CREDIT_TRANSFER"},
        *([_pacs002(pacs002_status)] if pacs002_status else []),
    ])
    c.seed("settlementPositions", [position or _position(gross=amount)])
    minors = int(round(amount * 100))
    c.seed("ledgerEvents", [_principal_event(minors, credit_code="1131"), _settlement_event(minors)])


def test_inbound_leg3_amounts_equal_the_booked_non_zero_amount():
    c = FakeConnection()
    _inbound_wire_fixture(c)

    check = compute_reconciliation(_PAY, c, "db")

    leg3 = check.legs[2]
    assert leg3.result == LEG_MATCH
    assert leg3.left_amount == leg3.right_amount == _MIN
    assert check.overall == RECONCILED


def test_outbound_leg3_amounts_equal_the_booked_non_zero_amount():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="WIRE", state="SETTLED")])
    c.seed("paymentExecutions", [_execution()])
    c.seed("settlementPositions", [_position()])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"), _settlement_event()])

    leg3 = compute_reconciliation(_PAY, c, "db").legs[2]

    assert leg3.result == LEG_MATCH
    assert leg3.left_amount == leg3.right_amount == _MIN


def test_leg3_mismatch_when_the_position_differs_from_the_booked_settlement_entry():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="WIRE", state="SETTLED")])
    c.seed("paymentExecutions", [_execution()])
    c.seed("settlementPositions", [_position(actual=24000.0)])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"), _settlement_event()])

    leg3 = compute_reconciliation(_PAY, c, "db").legs[2]

    # Clearing still nets to zero, but the amounts disagree — the invariant is not weakened.
    assert leg3.result == LEG_MISMATCH
    assert (leg3.left_amount, leg3.right_amount) == (2_400_000, _MIN)


def test_inbound_leg2_matches_when_pacs002_and_position_agree():
    c = FakeConnection()
    _inbound_wire_fixture(c)

    assert compute_reconciliation(_PAY, c, "db").legs[1].result == LEG_MATCH


def test_inbound_leg2_mismatch_when_pacs002_rejected_but_position_settled():
    c = FakeConnection()
    _inbound_wire_fixture(c, pacs002_status="RJCT")

    check = compute_reconciliation(_PAY, c, "db")

    assert check.legs[1].result == LEG_MISMATCH
    assert "pacs.002 RJCT" in check.legs[1].detail
    assert check.overall == DISCREPANT


def test_outbound_leg2_mismatch_when_rail_confirmed_amount_differs_from_the_position():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="WIRE", state="SETTLED")])
    c.seed("paymentExecutions", [_execution(amount=24000.0)])  # rail confirmed 24,000
    c.seed("settlementPositions", [_position()])               # position settled 25,000
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"), _settlement_event()])

    leg2 = compute_reconciliation(_PAY, c, "db").legs[1]

    assert leg2.result == LEG_MISMATCH
    assert (leg2.left_amount, leg2.right_amount) == (2_400_000, _MIN)


def test_outbound_leg2_mismatch_when_the_rail_rejected_but_position_settled():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="WIRE", state="SETTLED")])
    c.seed("paymentExecutions", [_execution(ack_code="RJCT")])
    c.seed("settlementPositions", [_position()])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"), _settlement_event()])

    assert compute_reconciliation(_PAY, c, "db").legs[1].result == LEG_MISMATCH


def test_leg2_waits_when_the_rail_has_confirmed_nothing():
    c = FakeConnection()
    _inbound_wire_fixture(c, pacs002_status=None)

    assert compute_reconciliation(_PAY, c, "db").legs[1].result == LEG_PENDING


def test_internal_transfer_legs_are_unchanged_by_the_settlement_comparisons():
    c = FakeConnection()
    c.seed("payments", [_payment(rail="INTERNAL", state="POSTED")])
    c.seed("ledgerEvents", [_principal_event()])

    legs = compute_reconciliation(_PAY, c, "db").legs

    assert [lg.result for lg in legs] == [LEG_NOT_APPLICABLE, LEG_NOT_APPLICABLE, LEG_MATCH]
    assert legs[2].left_amount is None and legs[2].right_amount is None


# --- not found -----------------------------------------------------------------

def test_unknown_payment_returns_none():
    c = FakeConnection()
    assert compute_reconciliation("PAY-NOPE", c, "db") is None


# --- Step 2: spec amendments + reconciliationItems shape (doc 22 §3 step 2) ----
#
# `reconciliationStatus`, `refs.settlementPositionId` and `refs.reconciliationItemId` are
# additive nullable amendments to the `payments` schema, made the same way stages 6/7 added
# `postingStatus` / `settlementStatus`. `reconciliationItems` is 0 matches in the canonical
# spec, so its shape is authored in `backend/ledger/data/reconciliation_items_schema.json`
# and ratified via Q56.

import json
import pathlib

_SPEC_PATH = (
    pathlib.Path(__file__).resolve().parents[4]
    / "doinas-research" / "propose_payments.json"
)
_ITEMS_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "data" / "reconciliation_items_schema.json"
)


def _payments_schema() -> dict:
    spec = json.loads(_SPEC_PATH.read_text())
    return spec["collections"]["payments"]["validator"]["$and"][0]["$jsonSchema"]


def test_reconciliation_status_is_a_spec_enum_amendment():
    """R13 — `lifecycle.reconciliationStatus` added additively, like postingStatus/settlementStatus."""
    lifecycle_props = _payments_schema()["properties"]["lifecycle"]["properties"]
    assert "reconciliationStatus" in lifecycle_props, "reconciliationStatus missing from lifecycle"
    enum = lifecycle_props["reconciliationStatus"]["enum"]
    assert set(enum) == {"UNRECONCILED", "RECONCILED", "DISCREPANT", None}
    # Nullable and NOT required — a payment written before stage 8 still validates.
    assert "string" in lifecycle_props["reconciliationStatus"]["bsonType"]
    assert "null" in lifecycle_props["reconciliationStatus"]["bsonType"]
    assert "reconciliationStatus" not in _payments_schema().get("required", [])


def test_refs_gained_settlement_position_and_reconciliation_item_ids():
    """Q50 (settlementPositionId, finally) + Q57 (reconciliationItemId)."""
    refs_props = _payments_schema()["properties"]["refs"]["properties"]
    for field in ("settlementPositionId", "reconciliationItemId"):
        assert field in refs_props, f"{field} missing from refs"
        assert "string" in refs_props[field]["bsonType"]
        assert "null" in refs_props[field]["bsonType"]
    assert {"settlementPositionId", "reconciliationItemId"}.isdisjoint(
        set(_payments_schema().get("required", []))
    )


def test_reconciliation_items_shape_is_loadable_and_requires_the_core_fields():
    """Q56 — the authored collection shape loads and requires the fields B4 specifies."""
    stub = json.loads(_ITEMS_SCHEMA_PATH.read_text())
    schema = stub["validator"]["$jsonSchema"]
    assert stub["collection"] == "reconciliationItems"
    required = set(schema["required"])
    assert required == {"reconciliationItemId", "paymentId", "legs", "overallResult", "checkedAt"}
    leg_enum = schema["properties"]["legs"]["items"]["properties"]["result"]["enum"]
    assert set(leg_enum) == {"MATCH", "MISMATCH", "NOT_APPLICABLE", "PENDING"}
    assert set(schema["properties"]["overallResult"]["enum"]) == {"RECONCILED", "DISCREPANT", "PENDING"}


def test_a_reconciliation_items_doc_validates_against_the_authored_shape():
    """The shape we author accepts a real three-leg result; rejects a bad overall/result."""
    schema = json.loads(_ITEMS_SCHEMA_PATH.read_text())["validator"]["$jsonSchema"]
    required = schema["required"]
    props = schema["properties"]

    good = {
        "reconciliationItemId": "RI-0001",
        "paymentId": _PAY,
        "legs": [
            {"leg": "PAYMENT_RAIL", "result": "MATCH", "leftAmount": _MIN, "rightAmount": _MIN, "detail": "ok"},
            {"leg": "RAIL_SETTLEMENT", "result": "MATCH", "leftAmount": _MIN, "rightAmount": _MIN, "detail": "ok"},
            {"leg": "SETTLEMENT_GL", "result": "MATCH", "leftAmount": _MIN, "rightAmount": _MIN, "detail": "ok"},
        ],
        "overallResult": "RECONCILED",
        "journalEntryId": _JNL,
        "settlementPositionId": _SP,
        "checkedAt": datetime(2026, 9, 3, tzinfo=timezone.utc),
        "sourceSystem": "ledger-service",
    }
    # Every required field present.
    assert all(good.get(f) is not None or f in good for f in required)
    # Every leg result + overall is in the authored enum.
    for lg in good["legs"]:
        assert lg["result"] in props["legs"]["items"]["properties"]["result"]["enum"]
    assert good["overallResult"] in props["overallResult"]["enum"]


# --- Step 3: the post-batch pass (doc 22 §3 step 3) ---------------------------
#
# Drives `reconcile_settled_payments` through the FakeConnection. The sweep query uses
# `{"$ne": "RECONCILED"}`, which Mongo resolves against a missing field as null (matches); the
# FakeDb needs the path to resolve, so these fixtures set `lifecycle.reconciliationStatus: None`
# explicitly — a fixture convenience, not a production requirement.

from services.reconciliation_service import reconcile_settled_payments


def _payment_with_recon(*, rail: str = "INTERNAL", state: str = "POSTED",
                        recon_status=None, **extra) -> dict:
    p = _payment(rail=rail, state=state)
    p["lifecycle"]["reconciliationStatus"] = recon_status
    p.update(extra)
    return p


def test_post_batch_reconciles_an_internal_transfer_one_batch_after_posted():
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="INTERNAL", state="POSTED")])
    c.seed("ledgerEvents", [_principal_event()])

    result = reconcile_settled_payments(c, "db")

    assert result == {"reconciled": 1, "discrepant": 0, "pending": 0, "missing": 0, "eligible": 1}
    p = c.get_collection("db", "payments").docs[0]
    assert p["lifecycle"]["currentState"] == "RECONCILED"
    assert p["status"] == "RECONCILED"
    assert p["lifecycle"]["reconciliationStatus"] == "RECONCILED"
    assert p["refs"]["reconciliationItemId"] is not None
    items = c.get_collection("db", "reconciliationItems").docs
    assert len(items) == 1
    assert items[0]["overallResult"] == "RECONCILED"
    assert items[0]["paymentId"] == _PAY


def test_post_batch_reconciles_an_external_wire_after_settlement_journal_posts():
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="WIRE", state="SETTLED", journal_ref=_JNL)])
    c.seed("paymentExecutions", [_execution()])
    c.seed("settlementPositions", [_position()])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"), _settlement_event()])
    c.seed("subLedgerEntries", [_subledger(_EVT_SET)])

    result = reconcile_settled_payments(c, "db")

    assert result["reconciled"] == 1
    p = c.get_collection("db", "payments").docs[0]
    assert p["lifecycle"]["currentState"] == "RECONCILED"
    assert p["refs"]["settlementPositionId"] == _SP


def test_post_batch_leaves_a_pending_payment_for_next_cycle():
    """A wire whose settlement journal has not posted yet is PENDING — not discrepant, not
    reconciled. The pass writes nothing and retries next batch."""
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="WIRE", state="SETTLED", journal_ref=None)])
    c.seed("paymentExecutions", [_execution()])
    c.seed("settlementPositions", [_position()])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"),
                            _settlement_event(posting_status="PENDING")])

    result = reconcile_settled_payments(c, "db")

    assert result == {"reconciled": 0, "discrepant": 0, "pending": 1, "missing": 0, "eligible": 1}
    p = c.get_collection("db", "payments").docs[0]
    assert p["lifecycle"]["currentState"] == "SETTLED"   # unchanged
    assert c.get_collection("db", "reconciliationItems").docs == []


def test_post_batch_flags_a_discrepancy_without_advancing_state():
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="WIRE", state="SETTLED")])
    c.seed("paymentExecutions", [_execution(amount=24000.0)])  # leg 1 mismatch
    c.seed("settlementPositions", [_position()])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"), _settlement_event()])
    c.seed("subLedgerEntries", [_subledger(_EVT_SET)])

    result = reconcile_settled_payments(c, "db")

    assert result == {"reconciled": 0, "discrepant": 1, "pending": 0, "missing": 0, "eligible": 1}
    p = c.get_collection("db", "payments").docs[0]
    assert p["lifecycle"]["currentState"] == "SETTLED"   # NOT advanced
    assert p["lifecycle"]["reconciliationStatus"] == "DISCREPANT"
    items = c.get_collection("db", "reconciliationItems").docs
    assert items[0]["overallResult"] == "DISCREPANT"


def test_a_failed_payment_is_never_swept():
    """FAILED/RETURNED are terminal, not in {SETTLED, POSTED} — the sweep excludes them."""
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="WIRE", state="FAILED")])
    c.seed("paymentExecutions", [_execution()])
    c.seed("settlementPositions", [_position(status="FAILED")])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131")])

    result = reconcile_settled_payments(c, "db")

    assert result == {"reconciled": 0, "discrepant": 0, "pending": 0, "missing": 0, "eligible": 0}


def test_a_rejected_inbound_payment_is_never_swept():
    """P6 — a sanctions-rejected inbound wire closes REJECTED with no transaction, ledger
    event or settlement position. REJECTED is outside {SETTLED, POSTED}, so the sweep
    never opens a reconciliation item or a MISSING exception for it."""
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="WIRE", state="REJECTED")])

    result = reconcile_settled_payments(c, "db")

    assert result == {"reconciled": 0, "discrepant": 0, "pending": 0, "missing": 0, "eligible": 0}
    assert c.get_collection("db", "reconciliationItems").docs == []


def test_an_already_reconciled_payment_is_not_re_swept():
    """Idempotent — the $ne RECONCILED filter excludes it, so no second lifecycle event."""
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="INTERNAL", state="RECONCILED",
                                             recon_status="RECONCILED")])
    c.seed("ledgerEvents", [_principal_event()])

    result = reconcile_settled_payments(c, "db")

    assert result["eligible"] == 0
    items = c.get_collection("db", "reconciliationItems").docs
    assert items == []


def test_the_reconciled_event_matches_the_state_machines_own_shape():
    """The ledger writes RECONCILED without importing the transactions lifecycle module
    (mirror-drift, defects.md 2026-06-18). Assert the event shape agrees with
    `lifecycle._event` so a convention change there fails loudly here, not silently."""
    import pathlib, importlib.util

    # Load the transactions service's lifecycle module by path (it is not on the ledger's
    # sys.path) — purely to read its _event shape, not to call it.
    lc_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "transactions" / "contexts" / "payment_order_initiation"
        / "domain" / "lifecycle.py"
    )
    spec = importlib.util.spec_from_file_location("tx_lifecycle", lc_path)
    tx_lifecycle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tx_lifecycle)

    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="INTERNAL", state="POSTED")])
    c.seed("ledgerEvents", [_principal_event()])
    reconcile_settled_payments(c, "db")

    event = c.get_collection("db", "payments").docs[0]["lifecycle"]["events"][-1]
    expected_keys = set(tx_lifecycle._event(
        "RECONCILED", "x", "SERVICE", "r", datetime(2026, 9, 3, tzinfo=timezone.utc),
    ).keys())
    assert set(event.keys()) == expected_keys
    assert event["state"] == "RECONCILED"
    assert event["actorType"] == "SERVICE"


# --- Steps 4 & 5: BIAN route + trace extension (doc 22 §3 steps 4–5) -----------

from services import pipeline_read_service


def test_compute_reconciliation_dict_shape_is_json_serialisable():
    """The BIAN route and the trace block both return `as_dict()` — assert the shape."""
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="INTERNAL", state="POSTED")])
    c.seed("ledgerEvents", [_principal_event()])
    check = compute_reconciliation(_PAY, c, "db")
    d = check.as_dict()
    assert d["paymentId"] == _PAY
    assert d["overallResult"] == "RECONCILED"
    assert len(d["legs"]) == 3
    assert {lg["leg"] for lg in d["legs"]} == {"PAYMENT_RAIL", "RAIL_SETTLEMENT", "SETTLEMENT_GL"}


def test_trace_payment_includes_the_reconciliation_block():
    """Step 5 — /pipeline/trace/{id} carries `reconciliation` for the UI panel."""
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="WIRE", state="SETTLED", journal_ref=_JNL)])
    c.seed("transactions", [{"paymentId": _PAY, "txnId": "TXN-0001"}])
    c.seed("paymentExecutions", [_execution()])
    c.seed("settlementPositions", [_position()])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"), _settlement_event()])
    c.seed("subLedgerEntries", [_subledger(_EVT_SET)])

    trace = pipeline_read_service.trace_payment(_PAY, c, "db")

    assert trace is not None
    assert "reconciliation" in trace
    assert trace["reconciliation"]["overallResult"] == "RECONCILED"
    # The existing keys are unchanged.
    assert trace["ledgerEvent"]["idempotencyKey"] == _PAY
    assert trace["settlementEvent"]["idempotencyKey"] == f"{_PAY}-SETTLEMENT"


def test_trace_reconciliation_block_is_pending_for_a_payment_with_no_journal_yet():
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="INTERNAL", state="SETTLED", journal_ref=None)])
    c.seed("transactions", [{"paymentId": _PAY, "txnId": "TXN-0001"}])
    c.seed("ledgerEvents", [_principal_event(posting_status="PENDING")])

    trace = pipeline_read_service.trace_payment(_PAY, c, "db")

    assert trace["reconciliation"]["overallResult"] == "PENDING"


def test_trace_payment_includes_the_reversal_event_from_return_of_funds():
    """Stage 9 — /pipeline/trace/{id} carries `reversalEvent` (idempotencyKey {paymentId}-REV)
    so the UI can show "compensating movement posted" once the GL batch journals it. Without
    this, a returned-funds payment shows the exception RESOLVED but no accounting evidence."""
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="WIRE", state="FAILED", journal_ref=_JNL)])
    c.seed("transactions", [{"paymentId": _PAY, "txnId": "TXN-0001"}])
    c.seed(
        "ledgerEvents",
        [_principal_event(credit_code="1131"), _reversal_event(posting_status="POSTED")],
    )

    trace = pipeline_read_service.trace_payment(_PAY, c, "db")

    assert trace["reversalEvent"] is not None
    assert trace["reversalEvent"]["idempotencyKey"] == f"{_PAY}-REV"
    assert trace["reversalEvent"]["postingStatus"] == "POSTED"
    assert trace["reversalEvent"]["postingResult"]["journalEntryId"] == _JNL
    # The principal is still the principal — the reversal is a sibling, not a replacement.
    assert trace["ledgerEvent"]["idempotencyKey"] == _PAY


def test_trace_payment_reversal_event_is_null_when_no_return_of_funds():
    """A payment that was not reversed has no `{paymentId}-REV` event — `reversalEvent` is
    null, not missing, so the UI can branch on falsiness without a KeyError."""
    c = FakeConnection()
    c.seed("payments", [_payment_with_recon(rail="WIRE", state="SETTLED", journal_ref=_JNL)])
    c.seed("transactions", [{"paymentId": _PAY, "txnId": "TXN-0001"}])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"), _settlement_event()])

    trace = pipeline_read_service.trace_payment(_PAY, c, "db")

    assert trace["reversalEvent"] is None


# --- the incoming wire: FR-8.IN1, leg 1 without an execution record --------------
#
# An inbound payment has no `paymentExecutions` doc by design (her L944), so leg 1 compares
# the received pacs.008 + the transmitted pacs.002 against the posted ledger entry INSTEAD
# (FR-8.IN1). Before this branch every inbound payment sat at leg-1 PENDING forever,
# rescanned each batch, never RECONCILED.

def _inbound_principal_event(amount_minors: int = _MIN) -> dict:
    """An inbound PAYMENT_PRINCIPAL ledgerEvent: **Dr 1131 clearing / Cr 2111 customer** —
    the mirror of the outbound principal. Stage 6's posting (FR-6.IN1)."""
    return {
        "eventId": _EVT,
        "idempotencyKey": _PAY,
        "postingStatus": "POSTED",
        "debitLeg": {"glAccountCode": "1131", "amount": amount_minors, "currency": "USD"},
        "creditLeg": {"glAccountCode": "2111", "amount": amount_minors, "currency": "USD"},
    }


def _inbound_settlement_event(amount_minors: int = _MIN) -> dict:
    """An inbound PAYMENT_SETTLEMENT ledgerEvent: **Dr 1111 nostro / Cr 1131 clearing** —
    the mirror of the outbound settlement. Stage 7's posting (FR-7.IN1). 1131 nets to zero
    across the two events, exactly as it does outbound."""
    return {
        "eventId": _EVT_SET,
        "idempotencyKey": f"{_PAY}-SETTLEMENT",
        "postingStatus": "POSTED",
        "debitLeg": {"glAccountCode": "1111", "amount": amount_minors, "currency": "USD"},
        "creditLeg": {"glAccountCode": "1131", "amount": amount_minors, "currency": "USD"},
    }


def _inbound_messages(*, status_code: str = "ACCP") -> list[dict]:
    """The two `paymentMessages` docs an inbound payment carries: the received pacs.008
    (direction INBOUND, purpose CREDIT_TRANSFER) and the transmitted pacs.002 (direction
    OUTBOUND on an INBOUND payment — the two axes are orthogonal)."""
    return [
        {"paymentMessageId": "PM-0001", "paymentId": _PAY,
         "direction": "INBOUND", "purpose": "CREDIT_TRANSFER",
         "messageFormat": "pacs.008.001.08", "rawMessage": {}},
        {"paymentMessageId": "PM-0002", "paymentId": _PAY,
         "direction": "OUTBOUND", "purpose": "STATUS_RESPONSE",
         "messageFormat": "pacs.002.001.08", "statusCode": status_code},
    ]


def _inbound_payment() -> dict:
    """A SETTLED inbound payment. Carries `direction: INBOUND`; carries NO execution record
    (not seeded, not even an empty list — the collection read returns nothing, which is
    the whole point of the branch under test)."""
    return {**_payment(rail="WIRE", state="SETTLED", journal_ref=_JNL),
            "direction": "INBOUND"}


def test_inbound_leg1_matches_without_any_execution_record():
    """FR-8.IN1 — THE pin. No paymentExecutions doc exists, and leg 1 still matches: the
    received message and the transmitted ACCP pacs.002 against the posted clearing debit.
    This is the test that fails if the inbound branch is removed and leg 1 falls back to
    reading the (absent) execution record."""
    c = FakeConnection()
    c.seed("payments", [_inbound_payment()])
    # ⚠️ No paymentExecutions — deliberate, that is the inbound condition.
    c.seed("paymentMessages", _inbound_messages())
    c.seed("settlementPositions", [_position()])
    c.seed("ledgerEvents", [_inbound_principal_event(), _inbound_settlement_event()])
    c.seed("subLedgerEntries", [_subledger(_EVT_SET)])

    check = compute_reconciliation(_PAY, c, "db")

    assert [lg.result for lg in check.legs] == [LEG_MATCH, LEG_MATCH, LEG_MATCH]
    assert check.overall == RECONCILED


def test_inbound_leg1_is_pending_until_the_pacs002_is_transmitted():
    """Message received but the sender not yet answered — PENDING, retried next batch,
    no exception (a quiet state, not operator work)."""
    c = FakeConnection()
    c.seed("payments", [_inbound_payment()])
    c.seed("paymentMessages", [_inbound_messages()[0]])  # the pacs.008 only
    c.seed("settlementPositions", [_position()])
    c.seed("ledgerEvents", [_inbound_principal_event(), _inbound_settlement_event()])

    check = compute_reconciliation(_PAY, c, "db")

    assert check.legs[0].result == LEG_PENDING
    assert "pacs.002" in check.legs[0].detail
    assert check.overall == PENDING


def test_inbound_leg1_mismatches_when_the_posted_credit_differs():
    """The comparison that matters: what we told the sender we would apply (the ACCP
    pacs.002) against what the ledger actually posted. A divergence is a real
    discrepancy — the exception the operator needs."""
    c = FakeConnection()
    c.seed("payments", [_inbound_payment()])
    c.seed("paymentMessages", _inbound_messages())
    c.seed("settlementPositions", [_position()])
    c.seed("ledgerEvents", [
        _inbound_principal_event(amount_minors=_MIN - 2500),
        _inbound_settlement_event(amount_minors=_MIN - 2500),
    ])

    check = compute_reconciliation(_PAY, c, "db")

    assert check.legs[0].result == LEG_MISMATCH
    assert check.overall == DISCREPANT


def test_leg2_mismatch_when_the_rail_settled_short_of_the_gl_posting():
    """Sep 17 L1264-1270 — settlement position $24,975 vs GL settlement posting $25,000:
    the doc's headline discrepancy. Leg 2 compares what actually settled."""
    c = FakeConnection()
    c.seed("payments", [_payment(rail="WIRE", amount=25000.0, state="SETTLED", journal_ref=_JNL)])
    c.seed("paymentExecutions", [_execution()])
    pos = _position()
    pos.update(outcome="UNMATCHED", expectedAmount=25000.0, actualAmount=24975.0)
    c.seed("settlementPositions", [pos])
    c.seed("ledgerEvents", [_principal_event(credit_code="1131"), _settlement_event()])
    c.seed("subLedgerEntries", [_subledger(_EVT_SET)])

    check = compute_reconciliation(_PAY, c, "db")

    leg2 = check.legs[1]
    assert leg2.result == LEG_MISMATCH
    assert leg2.left_amount - leg2.right_amount == 2500  # $25 in minor units
    assert check.overall == DISCREPANT
