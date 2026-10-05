"""Stage 9 — exceptions, repairs, and returns. Doc 24's gates.

Hermetic (FakeDb suite, no live cluster). Drives the real saga/route entries — never
monkeypatches a decision (defect 2026-09-01 `unreachable-control`).

This file holds the step-1 gate (the authored `exceptions` shape) and accrues the later
steps' gates as they land: step 2 the `record_exception` writer, step 3 the producers,
step 5 the resolve endpoint, step 6 the compensation path.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone

import types

import pytest

from process.exceptions import (
    ACTION_ACCEPT_DISCREPANCY,
    ACTION_DISMISS,
    ACTION_RECHECK,
    ACTION_LINK_STATEMENT_ENTRY,
    ACTION_POST_ADJUSTMENT,
    ACTION_ESCALATE_TO_CORRESPONDENT,
    ExceptionActionNotLegal,
    ExceptionConflict,
    ACTION_REPAIR,
    ACTION_RETRY_SETTLEMENT,
    ACTION_RETURN,
    ACTION_RETURN_FUNDS,
    CATEGORY_DUPLICATE_SIGNAL,
    CATEGORY_RECONCILIATION_DISCREPANCY,
    CATEGORY_SETTLEMENT_DELAYED,
    CATEGORY_SETTLEMENT_RETURNED,
    CATEGORY_SETTLEMENT_UNMATCHED,
    CATEGORY_UTA,
    OUTGOING_CATEGORIES,
    SERVICE_LEDGER,
    SERVICE_TRANSACTIONS,
    SEVERITY_ACTION_REQUIRED,
    SEVERITY_INFORMATIONAL,
    SOURCE_STAGE_RECONCILE,
    SOURCE_STAGE_SETTLE,
    SOURCE_STAGE_VALIDATE,
    STATUS_DISMISSED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    record_exception,
    severity_for,
)
from tests.test_payments_service import (  # reuse the fixtures, don't fork them
    FakeDb,
    _initiate,
    _initiate_external,
    db,          # noqa: F401 - pytest fixture
    service,     # noqa: F401 - pytest fixture
)
from contexts.payment_order_initiation.domain import lifecycle

# --- spec stub locations -----------------------------------------------------

_TRANSACTIONS_STUB = (
    pathlib.Path(__file__).resolve().parent.parent / "data" / "exceptions_schema.json"
)
_LEDGER_STUB = (
    pathlib.Path(__file__).resolve().parents[2] / "ledger" / "data" / "exceptions_schema.json"
)
_WORKING_V35 = (
    pathlib.Path(__file__).resolve().parents[4]
    / "Consolidated_Banking_DataModel_BIAN_v35_Sep10_WORKING.json"
)


def _stub_schema() -> dict:
    return json.loads(_TRANSACTIONS_STUB.read_text())["validator"]["$jsonSchema"]


# --- Step 1 gate: the authored shape loads, requires the core fields, and
#     its enums reject out-of-enum values ------------------------------------


def test_the_exceptions_stub_loads_and_names_the_collection():
    stub = json.loads(_TRANSACTIONS_STUB.read_text())
    assert stub["collection"] == "exceptions"
    schema = stub["validator"]["$jsonSchema"]
    assert schema["title"] == "PaymentException"
    assert set(schema["required"]) == {
        "exceptionId", "paymentId", "category", "status", "severity",
        "source", "sourceSystem", "createdAt", "updatedAt",
    }


def test_the_category_status_severity_action_enums_are_the_authored_set():
    p = _stub_schema()["properties"]
    assert set(p["category"]["enum"]) == {
        "SETTLEMENT_UNMATCHED", "SETTLEMENT_RETURNED", "SETTLEMENT_DELAYED",
        "RECONCILIATION_DISCREPANCY", "RECONCILIATION_MISSING", "ORPHANED_SETTLEMENT",
        "DUPLICATE_SIGNAL", "UTA",
    }
    assert set(p["status"]["enum"]) == {"OPEN", "RESOLVED", "DISMISSED"}
    assert set(p["severity"]["enum"]) == {"ACTION_REQUIRED", "INFORMATIONAL"}
    assert set(p["resolution"]["properties"]["action"]["enum"]) == {
        "RETRY_SETTLEMENT", "RETURN_FUNDS", "ACCEPT_DISCREPANCY", "DISMISS",
        "REPAIR", "RETURN", "RECHECK", "LINK_STATEMENT_ENTRY", "POST_ADJUSTMENT",
    }
    assert set(p["source"]["properties"]["stage"]["enum"]) == {
        "3 validate", "7 settle", "8 reconcile",
    }
    assert set(p["source"]["properties"]["service"]["enum"]) == {
        "transactions-service", "ledger-service",
    }
    # UTA / REPAIR / RETURN are reserved for the incoming flow and never written here.
    assert "UTA" in p["category"]["enum"]
    assert {"REPAIR", "RETURN"} <= set(p["resolution"]["properties"]["action"]["enum"])


def test_an_open_exception_doc_validates_and_out_of_enum_values_reject():
    schema = _stub_schema()
    props = schema["properties"]
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)

    good = {
        "exceptionId": "EXC-0001",
        "paymentId": "PAY-b3d97f83",
        "category": "SETTLEMENT_UNMATCHED",
        "status": "OPEN",
        "severity": "ACTION_REQUIRED",
        "source": {"stage": "7 settle", "service": "transactions-service"},
        "detail": {
            "discrepancyAmount": 25000.00,
            "discrepancyReason": "RJCT",
            "returnCode": None,
            "expectedAmount": 25000.00,
            "actualAmount": 0.0,
            "duplicateOf": None,
        },
        "resolution": None,
        "agent": None,
        "createdAt": now,
        "updatedAt": now,
        "sourceSystem": "transactions-service",
    }
    # every required field present
    assert all(good.get(f) is not None or f in good for f in schema["required"])
    # every enum field in its authored enum
    assert good["category"] in props["category"]["enum"]
    assert good["status"] in props["status"]["enum"]
    assert good["severity"] in props["severity"]["enum"]
    assert good["source"]["stage"] in props["source"]["properties"]["stage"]["enum"]
    assert good["source"]["service"] in props["source"]["properties"]["service"]["enum"]

    # out-of-enum rejection — the values the conformance walker would flag
    assert "PARTIAL_SETTLEMENT" not in props["category"]["enum"]
    assert "IN_PROGRESS" not in props["status"]["enum"]
    assert "BLOCKED" not in props["severity"]["enum"]
    assert "REVERSE" not in props["resolution"]["properties"]["action"]["enum"]

    # a resolved doc carries a legal resolution
    resolved = dict(good)
    resolved["status"] = "RESOLVED"
    resolved["resolution"] = {
        "action": "ACCEPT_DISCREPANCY",
        "by": "payments-operations",
        "at": now,
        "note": "Correspondent fee — accepted with cause.",
    }
    assert resolved["resolution"]["action"] in props["resolution"]["properties"]["action"]["enum"]


def test_the_ledger_mirror_is_byte_identical_to_the_canonical_stub():
    """B3 mirror-drift guard: two services write one collection, one shape. The ledger's
    copy of the stub must stay byte-identical to the transactions canonical copy — the
    test is the contract (no shared import path across services)."""
    assert _LEDGER_STUB.exists(), "ledger mirror missing"
    assert _TRANSACTIONS_STUB.read_text() == _LEDGER_STUB.read_text(), (
        "exceptions_schema.json drifted between transactions and ledger — copy byte-identical"
    )


def test_the_working_v35_validator_matches_the_authored_stub():
    """Lockstep: the consolidated working spec's `exceptions` validator is the authored
    stub's validator, verbatim — the spec-stub and the consolidated model must not drift."""
    v35 = json.loads(_WORKING_V35.read_text())
    assert "exceptions" in v35["collections"], "exceptions not yet in working v35"
    assert v35["meta"]["collectionCount"] == 27
    assert "exceptions" in v35["meta"]["collectionGroups"]["payments_platform"]
    assert v35["collections"]["exceptions"]["validator"] == json.loads(_TRANSACTIONS_STUB.read_text())["validator"]


# --- Step 2 gate: the record_exception writer --------------------------------
#
# `record_exception(collections, payment, category, detail, source)` — dedupes on
# (paymentId, category, OPEN), derives EXC-…, inserts via collections.db["exceptions"].


def _collections():
    """A PaymentCollections stand-in: only `.db` is touched, and FakeDb.__missing__ yields
    the `exceptions` collection for free (doc 24 §3 step 2)."""
    return types.SimpleNamespace(db=FakeDb({}))


def _payment(**over):
    base = {"paymentId": "PAY-b3d97f83", "amount": 25000.00, "currency": "USD"}
    base.update(over)
    return base


def test_the_code_enum_constants_match_the_authored_stub():
    """2026-04-28 enum-drift prevention: every tuple in process/exceptions.py equals the
    stub's enum array. Both services' writers are walked by this same pattern (B3 mirror
    guard) — the ledger side has its own twin constants asserted against the same stub."""
    p = _stub_schema()["properties"]
    assert set(OUTGOING_CATEGORIES) == set(p["category"]["enum"]) - {CATEGORY_UTA}
    assert {STATUS_OPEN, STATUS_RESOLVED, STATUS_DISMISSED} == set(p["status"]["enum"])
    assert {SEVERITY_ACTION_REQUIRED, SEVERITY_INFORMATIONAL} == set(p["severity"]["enum"])
    assert {
        ACTION_RETRY_SETTLEMENT, ACTION_RETURN_FUNDS, ACTION_ACCEPT_DISCREPANCY,
        ACTION_DISMISS, ACTION_REPAIR, ACTION_RETURN, ACTION_RECHECK,
        ACTION_LINK_STATEMENT_ENTRY, ACTION_POST_ADJUSTMENT,
    } == set(p["resolution"]["properties"]["action"]["enum"])
    assert {
        SOURCE_STAGE_VALIDATE, SOURCE_STAGE_SETTLE, SOURCE_STAGE_RECONCILE,
    } == set(p["source"]["properties"]["stage"]["enum"])
    assert {SERVICE_TRANSACTIONS, SERVICE_LEDGER} == set(p["source"]["properties"]["service"]["enum"])


def test_record_exception_inserts_one_open_doc_with_the_authored_shape():
    c = _collections()
    doc = record_exception(
        c, _payment(),
        CATEGORY_SETTLEMENT_UNMATCHED,
        detail={"discrepancyAmount": 25000.00, "expectedAmount": 25000.00, "actualAmount": 0.0,
                "discrepancyReason": "RJCT", "returnCode": None, "duplicateOf": None},
        source={"stage": SOURCE_STAGE_SETTLE, "service": SERVICE_TRANSACTIONS},
    )
    excs = c.db["exceptions"].docs
    assert len(excs) == 1
    assert doc["exceptionId"].startswith("EXC-")
    assert len(doc["exceptionId"]) == len("EXC-") + 8
    assert doc["paymentId"] == "PAY-b3d97f83"
    assert doc["category"] == "SETTLEMENT_UNMATCHED"
    assert doc["status"] == STATUS_OPEN
    assert doc["severity"] == SEVERITY_ACTION_REQUIRED
    assert doc["source"] == {"stage": "7 settle", "service": "transactions-service"}
    assert doc["sourceSystem"] == "transactions-service"
    assert doc["resolution"] is None
    assert doc["agent"] is None
    assert doc["detail"]["discrepancyAmount"] == 25000.00


@pytest.mark.parametrize("category,severity", [
    (CATEGORY_SETTLEMENT_UNMATCHED, SEVERITY_ACTION_REQUIRED),
    (CATEGORY_SETTLEMENT_RETURNED, SEVERITY_ACTION_REQUIRED),
    (CATEGORY_SETTLEMENT_DELAYED, SEVERITY_ACTION_REQUIRED),
    (CATEGORY_RECONCILIATION_DISCREPANCY, SEVERITY_ACTION_REQUIRED),
    (CATEGORY_DUPLICATE_SIGNAL, SEVERITY_INFORMATIONAL),
])
def test_severity_follows_the_b3_table(category, severity):
    assert severity_for(category) == severity


def test_record_exception_dedupes_on_paymentId_category_open():
    """Site 4 re-checks DISCREPANT payments every batch pass — the writer must not
    double-insert (B3). A second call for the same (paymentId, category) returns the
    existing OPEN doc and inserts nothing."""
    c = _collections()
    first = record_exception(
        c, _payment(), CATEGORY_RECONCILIATION_DISCREPANCY, detail={"discrepancyAmount": 25.0},
        source={"stage": SOURCE_STAGE_RECONCILE, "service": SERVICE_LEDGER},
    )
    second = record_exception(
        c, _payment(), CATEGORY_RECONCILIATION_DISCREPANCY, detail={"discrepancyAmount": 25.0},
        source={"stage": SOURCE_STAGE_RECONCILE, "service": SERVICE_LEDGER},
    )
    assert second["exceptionId"] == first["exceptionId"]
    assert len(c.db["exceptions"].docs) == 1


def test_record_exception_does_not_dedupe_across_categories():
    """A payment can fail AND later draw a duplicate signal — occurrence-per-doc (DR-5.1)."""
    c = _collections()
    record_exception(
        c, _payment(), CATEGORY_SETTLEMENT_UNMATCHED, detail={"discrepancyAmount": 25000.0},
        source={"stage": SOURCE_STAGE_SETTLE, "service": SERVICE_TRANSACTIONS},
    )
    record_exception(
        c, _payment(), CATEGORY_DUPLICATE_SIGNAL, detail={"duplicateOf": "PAY-aaaa0001"},
        source={"stage": SOURCE_STAGE_VALIDATE, "service": SERVICE_TRANSACTIONS},
    )
    assert len(c.db["exceptions"].docs) == 2


def test_record_exception_rejects_the_reserved_incoming_category():
    """UTA is reserved for the incoming Repair/Return flow — the outgoing path never
    writes it (FR-9.IN4)."""
    c = _collections()
    with pytest.raises(ValueError):
        record_exception(
            c, _payment(), CATEGORY_UTA, detail=None,
            source={"stage": SOURCE_STAGE_SETTLE, "service": SERVICE_TRANSACTIONS},
        )


def test_record_exception_requires_a_payment_id():
    c = _collections()
    with pytest.raises(ValueError):
        record_exception(
            c, {"paymentId": None}, CATEGORY_SETTLEMENT_UNMATCHED, detail=None,
            source={"stage": SOURCE_STAGE_SETTLE, "service": SERVICE_TRANSACTIONS},
        )



# --- Step 3 gate: producers fire at detection time (hermetic, real saga) ------
#
# Each test drives the real PaymentsService saga (defect 2026-09-01
# `unreachable-control`: only an end-to-end drive proves the exception is created).

def _exceptions(db):
    return getattr(db.get("exceptions"), "docs", None) or []


def _db_payment(db, *, idx=0):
    pays = db["payments"].docs
    return pays[idx]


def test_an_unmatched_wire_settles_with_no_stage7_exception(service, db):
    """Sep 17 L1264-1270: an unmatched settlement still SETTLES — the GL posts the full
    amount and stage 8 raises the discrepancy. Stage 7 opens no exception."""
    from contexts.payment_settlement import settle
    from tests.test_payments_service import FakeConnection

    _initiate_external(service, settlement_outcome="UNMATCHED")
    payment = _db_payment(db)
    assert payment["lifecycle"]["currentState"] == lifecycle.IN_PROGRESS
    assert payment["lifecycle"]["settlementStatus"] == "PENDING"
    assert _exceptions(db) == []

    assert settle.complete_due(FakeConnection(db), "leafy_bank_bian", delay_seconds=0) == 1
    settled = db["payments"].find_one({"paymentId": payment["paymentId"]})
    assert settled["lifecycle"]["currentState"] == lifecycle.SETTLED
    assert settled["lifecycle"]["settlementStatus"] == "SETTLED"

def test_unmatched_is_an_alias_for_a_fee_deducted_statement_line(service, db):
    """Reconciliation plan Decision 1 — the statement is the only source of the $25.
    UNMATCHED settles like MATCHED, writes no discrepancy and no actual amount of its own,
    and books the correspondent's line as FEE_DEDUCTED (camt053 caps the charge at the
    amount, so a sub-$25 wire cannot go negative)."""
    _initiate_external(service, settlement_outcome="UNMATCHED")
    payment = _db_payment(db)

    assert payment["simulatedStatementOutcome"] == "FEE_DEDUCTED"
    assert payment["clearing"]["discrepancyAmount"] is None
    pos = db["settlementPositions"].find_one({"paymentId": payment["paymentId"]})
    assert pos["outcome"] == "UNMATCHED"
    assert pos["settlementStatus"] == "SETTLED"
    assert pos["expectedAmount"] == payment["amount"]
    assert pos["actualAmount"] is None, "A2: only the statement line supplies the actual"


def test_an_exception_wire_ends_returned_with_an_open_settlement_returned_exception(service, db):
    _initiate_external(service, settlement_outcome="EXCEPTION")
    payment = _db_payment(db)
    assert payment["lifecycle"]["currentState"] == lifecycle.RETURNED
    excs = _exceptions(db)
    assert len(excs) == 1
    assert excs[0]["category"] == CATEGORY_SETTLEMENT_RETURNED
    assert excs[0]["detail"]["returnCode"] == "RETURNED_EXCEPTION"


def test_a_delayed_wire_holds_at_in_progress_with_a_settlement_delayed_exception(service, db):
    _initiate_external(service, settlement_outcome="DELAYED")
    payment = _db_payment(db)
    assert payment["lifecycle"]["currentState"] == lifecycle.IN_PROGRESS
    excs = _exceptions(db)
    assert len(excs) == 1
    assert excs[0]["category"] == CATEGORY_SETTLEMENT_DELAYED
    assert excs[0]["status"] == STATUS_OPEN
    # DELAYED's actual is None — settlement not yet confirmed
    assert excs[0]["detail"]["actualAmount"] is None


def test_a_content_duplicate_proceeds_and_queues_an_informational_duplicate_signal(service, db):
    """Two identical external wires within the duplicate window. The second is NOT refused
    (a repeat payment can be legitimate) and draws a DUPLICATE_SIGNAL queue row — the only
    INFORMATIONAL category (B3 site 5)."""
    first = _initiate_external(service)
    second = _initiate_external(service)
    # both proceed — neither is REJECTED
    assert first["paymentId"] != second["paymentId"]
    assert second["lifecycle"]["currentState"] != lifecycle.REJECTED
    excs = _exceptions(db)
    dup = [e for e in excs if e["category"] == CATEGORY_DUPLICATE_SIGNAL]
    assert len(dup) == 1
    assert dup[0]["severity"] == SEVERITY_INFORMATIONAL
    assert dup[0]["paymentId"] == second["paymentId"]
    assert dup[0]["detail"]["duplicateOf"] == first["paymentId"]
    assert dup[0]["source"] == {"stage": "3 validate", "service": "transactions-service"}


def test_a_same_key_replay_returns_the_winner_with_an_evidence_check_and_no_exception(service, db):
    """R3 — a same-key second submission returns the winner, appends an
    `idempotent_replay_absorbed` PASS check on it, and creates NO exception (a replay is
    not an exception; it is idempotency doing its job)."""
    first = _initiate_external(service, idempotency_key="KEY-001")
    second = _initiate_external(service, idempotency_key="KEY-001")
    assert second["paymentId"] == first["paymentId"]
    # exactly one payment document
    assert len(db["payments"].docs) == 1
    # no exception queued for a replay
    assert _exceptions(db) == []
    # the winner carries the evidence check
    winner = db["payments"].docs[0]
    replay_checks = [c for c in winner.get("checks", []) if c.get("name") == "idempotent_replay_absorbed"]
    assert len(replay_checks) == 1
    assert replay_checks[0]["result"] == "PASS"
    assert "KEY-001" in replay_checks[0]["detail"]
    assert first["paymentId"] in replay_checks[0]["detail"]


def test_a_clean_matched_wire_creates_no_exception(service, db):
    """Regression — the happy path is unchanged: a MATCHED external wire settles with no
    exception row (doc 24 §4 Regression)."""
    from contexts.payment_settlement import settle
    from tests.test_payments_service import FakeConnection
    _initiate_external(service, settlement_outcome="MATCHED")
    settle.complete_due(FakeConnection(db), "leafy_bank_bian", delay_seconds=0)  # deferred completion (36215b1)
    assert _exceptions(db) == []
    assert _db_payment(db)["lifecycle"]["currentState"] == lifecycle.SETTLED


def test_an_internal_transfer_creates_no_exception(service, db):
    """Regression — a book transfer settles atomically in stage 5; settle is a no-op and
    queues nothing."""
    _initiate(service)  # INTERNAL
    assert _exceptions(db) == []
    assert _db_payment(db)["lifecycle"]["currentState"] == lifecycle.SETTLED


# --- Step 6 gate: compensation return_of_funds (the milestone) ----------------
#
# Drives a RETURNED external wire through the real saga, then calls
# `compensation.return_of_funds` and asserts the debtor is restored, 1131 is cleared, a
# compensating transactions doc (reversalOf set) is written, and the payment's terminal
# state is NOT touched (B4 — FAILED/RETURNED stay final; resolution is evidence).

from process.compensation import return_of_funds
from process.payment_context import PaymentCollections


def _collections_from(db):
    return PaymentCollections(
        db=db,
        customers=db["customers"],
        accounts=db["accounts"],
        payments=db["payments"],
        transactions=db["transactions"],
        notifications=db["notifications"],
    )


def test_return_of_funds_restores_the_debtor_and_clears_1131_with_a_compensating_txn(service, db):
    _initiate_external(service, settlement_outcome="EXCEPTION")
    payment = _db_payment(db)
    assert payment["lifecycle"]["currentState"] == lifecycle.RETURNED

    original_txn = db["transactions"].find_one({"paymentId": payment["paymentId"]})
    assert original_txn is not None, "external wire must have a boundary transactions doc"
    original_amount = original_txn["amount"]
    debtor_id = original_txn["payer"]["accountId"]
    clearing_id = original_txn["payee"]["accountId"]

    debtor_before = db["accounts"].find_one({"accountId": debtor_id})["balance"]["current"]
    clearing_before = db["accounts"].find_one({"accountId": clearing_id})["balance"]["current"]
    # the saga debited the debtor and credited the clearing account
    assert debtor_before == 10_000.0 - original_amount
    assert clearing_before == 0 + original_amount

    exception = _exceptions(db)[0]
    colls = _collections_from(db)
    rev_doc = return_of_funds(colls, payment, exception)

    # balances restored to pre-saga
    debtor_after = db["accounts"].find_one({"accountId": debtor_id})["balance"]["current"]
    clearing_after = db["accounts"].find_one({"accountId": clearing_id})["balance"]["current"]
    assert debtor_after == 10_000.0
    assert clearing_after == 0.0

    # one compensating transactions doc, reversalOf pointing at the original
    assert rev_doc["reversalOf"] == original_txn["txnId"]
    assert rev_doc["txnId"] != original_txn["txnId"]
    txns = [t for t in db["transactions"].docs if t["paymentId"] == payment["paymentId"]]
    assert len(txns) == 2  # original + compensating
    # the original is byte-unchanged (R7) — its reversalOf is still absent/None
    assert original_txn.get("reversalOf") is None

    # evidence on the payment; terminal state NOT changed (B4)
    payment_after = db["payments"].find_one({"paymentId": payment["paymentId"]})
    comp_checks = [c for c in payment_after.get("checks", []) if c.get("name") == "compensation_posted"]
    assert len(comp_checks) == 1
    assert comp_checks[0]["result"] == "PASS"
    assert payment_after["lifecycle"]["currentState"] == lifecycle.RETURNED


def test_return_of_funds_raises_when_no_boundary_txn_exists(service, db):
    """A payment with no transactions doc (e.g. a REJECTED-at-initiation payment) cannot be
    reversed — there is nothing to undo. The guard raises rather than silently no-op."""
    _initiate_external(service, settlement_outcome="EXCEPTION")
    payment = _db_payment(db)
    # wipe the transactions doc to simulate a payment that never moved money
    db["transactions"].docs.clear()
    colls = _collections_from(db)
    import pytest as _pytest
    with _pytest.raises(ValueError):
        return_of_funds(colls, payment, {"exceptionId": "EXC-test"})


# --- Step 5 gate: the resolve endpoint (service-level, the repo pattern) ----------
#
# Drives `service.resolve_exception` directly (the route is a thin guard→service call; the
# route maps the ValueErrors to 404/409/422). Each action end-to-end through the real service.

def _exc(db, *, idx=0):
    excs = _exceptions(db)
    return excs[idx]


def test_resolve_retry_on_delayed_settles_and_auto_resolves(service, db):
    _initiate_external(service, settlement_outcome="DELAYED")
    exc = _exc(db)
    assert exc["category"] == CATEGORY_SETTLEMENT_DELAYED
    payment = _db_payment(db)
    assert payment["lifecycle"]["currentState"] == lifecycle.IN_PROGRESS

    updated = service.resolve_exception(
        exc["exceptionId"], action=ACTION_RETRY_SETTLEMENT, new_settlement_outcome="MATCHED",
    )

    assert updated["status"] == STATUS_RESOLVED
    assert updated["resolution"]["action"] == ACTION_RETRY_SETTLEMENT
    assert updated["resolution"]["by"] == "payments-operations"
    # the retry landed MATCHED → payment SETTLED
    payment_after = db["payments"].find_one({"paymentId": payment["paymentId"]})
    assert payment_after["lifecycle"]["currentState"] == lifecycle.SETTLED


def _discrepant_wire(service, db, *, charge_bearer=None):
    """A settled external wire carrying the stage-8 RECONCILIATION_DISCREPANCY, stamped as
    `_stamp_discrepant` does (the ledger's sweep is not in this suite)."""
    from process.exceptions import record_exception

    _initiate_external(service, settlement_outcome="MATCHED")
    payment = _db_payment(db)
    pid = payment["paymentId"]
    extra = {"lifecycle.reconciliationStatus": "DISCREPANT",
             "lifecycle.currentState": lifecycle.SETTLED}
    if charge_bearer:
        extra["chargeBearer"] = charge_bearer
    db["payments"].update_one({"paymentId": pid}, {"$set": extra})
    payment = db["payments"].find_one({"paymentId": pid})
    record_exception(
        service._collections(), payment, CATEGORY_RECONCILIATION_DISCREPANCY,
        detail={"discrepancyAmount": 25.0, "discrepancyReason": "rail settled short",
                "expectedAmount": 25000.0, "actualAmount": 24975.0,
                "returnCode": None, "duplicateOf": None},
        source={"stage": SOURCE_STAGE_RECONCILE, "service": SERVICE_LEDGER},
    )
    return payment, _exc(db)


def test_post_adjustment_on_a_debt_wire_stamps_the_correction_for_the_ledger(service, db):
    """Plan A4 — chargeBearer DEBT: the bank bears the correspondent's charge, so the
    approved POST_ADJUSTMENT stamps the correction the ledger posts as Dr 5214 / Cr nostro.
    It does NOT flip reconciliationStatus: reconciliation nets the -ADJ event and closes the
    leg itself (D2), held PENDING meanwhile by the position's `adjustmentPending`."""
    payment, exc = _discrepant_wire(service, db, charge_bearer="DEBT")
    db["settlementPositions"].insert_one({"paymentId": payment["paymentId"]})

    updated = service.resolve_exception(exc["exceptionId"], action=ACTION_POST_ADJUSTMENT,
                                        note="Correspondent fee — bank absorbs.")

    assert updated["status"] == STATUS_RESOLVED
    assert updated["resolution"]["action"] == ACTION_POST_ADJUSTMENT
    after = db["payments"].find_one({"paymentId": payment["paymentId"]})
    adj = after["clearing"]["settlementAdjustment"]
    assert adj["amount"] == 25.0
    assert adj["chargeBearer"] == "DEBT"
    assert adj["exceptionId"] == exc["exceptionId"]
    assert after["lifecycle"]["reconciliationStatus"] == "DISCREPANT"
    position = db["settlementPositions"].find_one({"paymentId": payment["paymentId"]})
    assert position["adjustmentPending"] is True


def test_a_second_post_adjustment_is_refused_and_stamps_nothing_twice(service, db):
    payment, exc = _discrepant_wire(service, db, charge_bearer="DEBT")
    service.resolve_exception(exc["exceptionId"], action=ACTION_POST_ADJUSTMENT)
    # A racing resolver that read the exception while it was still OPEN.
    with pytest.raises(ExceptionConflict):
        service._post_adjustment(payment, exc, None, datetime.now(timezone.utc))


def test_accept_on_a_debt_wire_is_refused(service, db):
    """A4 D3 — DEBT's only closing action is POST_ADJUSTMENT."""
    payment, exc = _discrepant_wire(service, db, charge_bearer="DEBT")
    with pytest.raises(ExceptionActionNotLegal, match="POST_ADJUSTMENT"):
        service.resolve_exception(exc["exceptionId"], action=ACTION_ACCEPT_DISCREPANCY)
    assert _exc(db)["status"] == STATUS_OPEN
    after = db["payments"].find_one({"paymentId": payment["paymentId"]})
    assert "settlementAdjustment" not in (after.get("clearing") or {})


@pytest.mark.parametrize("bearer", ["CRED", "SHAR", "SLEV"])
def test_post_adjustment_is_refused_when_the_beneficiary_bears_charges(service, db, bearer):
    payment, exc = _discrepant_wire(service, db, charge_bearer=bearer)
    with pytest.raises(ExceptionActionNotLegal, match="ACCEPT_DISCREPANCY"):
        service.resolve_exception(exc["exceptionId"], action=ACTION_POST_ADJUSTMENT)
    assert _exc(db)["status"] == STATUS_OPEN


@pytest.mark.parametrize("action", ["RECHECK", "LINK_STATEMENT_ENTRY"])
def test_ledger_owned_actions_point_at_the_ledger_route(service, db, action):
    _, exc = _discrepant_wire(service, db, charge_bearer="SHAR")
    with pytest.raises(ExceptionActionNotLegal, match="/pipeline/exceptions/"):
        service.resolve_exception(exc["exceptionId"], action=action)


def test_escalate_sends_one_investigation_request_and_keeps_the_exception_open(service, db):
    payment, exc = _discrepant_wire(service, db, charge_bearer="SHAR")

    updated = service.resolve_exception(
        exc["exceptionId"], action=ACTION_ESCALATE_TO_CORRESPONDENT, note="Please confirm fee.")

    assert updated["status"] == STATUS_OPEN
    assert updated["awaitingCounterparty"] is True
    msg = db["paymentMessages"].find_one(
        {"paymentMessageId": updated["escalation"]["paymentMessageId"]})
    assert msg["purpose"] == "INVESTIGATION_REQUEST"
    assert msg["messageFormat"].startswith("camt.026")
    assert msg["payload"]["Document"]["UblToApply"]["Undrlyg"]["OrgnlEndToEndId"] == payment["paymentId"]
    with pytest.raises(ExceptionConflict):
        service.resolve_exception(exc["exceptionId"], action=ACTION_ESCALATE_TO_CORRESPONDENT)
    assert db["paymentMessages"].count_documents({"purpose": "INVESTIGATION_REQUEST"}) == 1
    # Still closable afterwards by the bearer-correct action.
    assert service.resolve_exception(
        exc["exceptionId"], action=ACTION_ACCEPT_DISCREPANCY)["status"] == STATUS_RESOLVED


def test_an_orphan_line_escalates_and_dismisses_without_a_payment(service, db):
    """ORPHANED_SETTLEMENT has no payment (A3 D1a key `<msgId>#<lineNo>`), seeded here the
    way the ledger's raise_orphans writes it."""
    now = datetime.now(timezone.utc)
    db["exceptions"].insert_one({
        "_id": "oid-orph", "exceptionId": "EXC-ORPH", "paymentId": "PM-STMT#3", "category": "ORPHANED_SETTLEMENT",
        "status": STATUS_OPEN, "severity": "ACTION_REQUIRED",
        "source": {"stage": SOURCE_STAGE_RECONCILE, "service": SERVICE_LEDGER},
        "detail": {"actualAmount": 1250.0, "reference": "8EBAA746/LEAFYBK",
                   "discrepancyReason": "Correspondent statement line with no matching payment."},
        "subjectRef": {"kind": "STATEMENT_LINE", "paymentMessageId": "PM-STMT", "lineNo": 3},
        "resolution": None, "agent": None, "createdAt": now, "updatedAt": now,
        "sourceSystem": SERVICE_LEDGER,
    })

    escalated = service.resolve_exception("EXC-ORPH", action=ACTION_ESCALATE_TO_CORRESPONDENT)
    msg = db["paymentMessages"].find_one({"purpose": "INVESTIGATION_REQUEST"})
    assert msg["paymentId"] is None
    assert msg["payload"]["Document"]["UblToApply"]["Undrlyg"] == {
        "StmtRef": "PM-STMT", "NtryNb": 3, "NtryRef": "8EBAA746/LEAFYBK"}
    assert escalated["status"] == STATUS_OPEN

    dismissed = service.resolve_exception("EXC-ORPH", action=ACTION_DISMISS,
                                          note="Confirmed another bank's entry.")
    assert dismissed["status"] == STATUS_DISMISSED


def test_an_orphan_line_a_settled_wire_is_waiting_for_cannot_be_dismissed(service, db):
    """R2 (2026-10-05): dismissing a re-keyed line stranded its wire as MISSING."""
    now = datetime.now(timezone.utc)
    db["paymentMessages"].insert_one({
        "paymentMessageId": "PM-STMT", "purpose": "ACCOUNT_STATEMENT",
        "statement": {"accountCode": "1111"},
        "entries": [{"lineNo": 1, "reference": "1BCF3CC0/LEAFYBK", "amount": 6120.0}]})
    db["settlementPositions"].insert_one({
        "paymentId": "PAY-WAIT", "settlementAccountCode": "1111",
        "expectedAmount": 6120.0, "actualAmount": None})
    db["payments"].insert_one({"paymentId": "PAY-WAIT", "lifecycle": {
        "currentState": "SETTLED", "reconciliationStatus": "PENDING"}})
    db["exceptions"].insert_one({
        "_id": "oid-orph2", "exceptionId": "EXC-ORPH2", "paymentId": "PM-STMT#1",
        "category": "ORPHANED_SETTLEMENT", "status": STATUS_OPEN, "severity": "ACTION_REQUIRED",
        "source": {"stage": SOURCE_STAGE_RECONCILE, "service": SERVICE_LEDGER},
        "detail": {}, "subjectRef": {"kind": "STATEMENT_LINE", "paymentMessageId": "PM-STMT", "lineNo": 1},
        "resolution": None, "agent": None, "createdAt": now, "updatedAt": now,
        "sourceSystem": SERVICE_LEDGER})

    with pytest.raises(ExceptionConflict, match="PAY-WAIT"):
        service.resolve_exception("EXC-ORPH2", action=ACTION_DISMISS)
    assert db["exceptions"].find_one({"exceptionId": "EXC-ORPH2"})["status"] == STATUS_OPEN


@pytest.mark.parametrize("bearer", ["CRED", "SHAR", "SLEV"])
def test_accept_when_the_beneficiary_bears_charges_posts_nothing(service, db, bearer):
    """CRED/SHAR/SLEV — the short credit is the beneficiary's; the accept is the record."""
    payment, exc = _discrepant_wire(service, db, charge_bearer=bearer)
    updated = service.resolve_exception(exc["exceptionId"], action=ACTION_ACCEPT_DISCREPANCY)
    assert updated["status"] == STATUS_RESOLVED
    after = db["payments"].find_one({"paymentId": payment["paymentId"]})
    assert "settlementAdjustment" not in (after.get("clearing") or {})
    assert after["lifecycle"]["reconciliationStatus"] == "RECONCILED"

def test_resolve_dismiss_closes_an_informational_duplicate_signal(service, db):
    _initiate_external(service)
    _initiate_external(service)  # second draws the DUPLICATE_SIGNAL
    dup = [e for e in _exceptions(db) if e["category"] == CATEGORY_DUPLICATE_SIGNAL][0]

    updated = service.resolve_exception(dup["exceptionId"], action=ACTION_DISMISS)

    assert updated["status"] == STATUS_DISMISSED
    assert updated["resolution"]["action"] == ACTION_DISMISS


def test_resolve_return_funds_restores_the_debtor(service, db):
    _initiate_external(service, settlement_outcome="EXCEPTION")
    exc = _exc(db)
    assert exc["category"] == CATEGORY_SETTLEMENT_RETURNED
    payment = _db_payment(db)
    debtor_id = payment["debtor"]["accountId"]
    debtor_held = db["accounts"].find_one({"accountId": debtor_id})["balance"]["current"]

    updated = service.resolve_exception(exc["exceptionId"], action=ACTION_RETURN_FUNDS)

    assert updated["status"] == STATUS_RESOLVED
    # debtor restored to pre-saga balance (10_000)
    debtor_after = db["accounts"].find_one({"accountId": debtor_id})["balance"]["current"]
    assert debtor_after == 10_000.0
    # compensation evidence on the payment
    payment_after = db["payments"].find_one({"paymentId": payment["paymentId"]})
    assert any(c.get("name") == "compensation_posted" for c in payment_after.get("checks", []))


def test_resolve_404_unknown_exception(service, db):
    with pytest.raises(ValueError, match="not found"):
        service.resolve_exception("EXC-nonexistent", action=ACTION_DISMISS)


def test_resolve_409_on_an_already_resolved_exception(service, db):
    _, exc = _discrepant_wire(service, db)
    service.resolve_exception(exc["exceptionId"], action=ACTION_ACCEPT_DISCREPANCY)
    # a second resolve on the now-RESOLVED exception → 409
    with pytest.raises(ValueError, match="not OPEN"):
        service.resolve_exception(exc["exceptionId"], action=ACTION_ACCEPT_DISCREPANCY)


def test_resolve_422_action_not_legal_for_category(service, db):
    _, exc = _discrepant_wire(service, db)
    # DISMISS is legal only for DUPLICATE_SIGNAL, not RECONCILIATION_DISCREPANCY
    with pytest.raises(ValueError, match="not legal"):
        service.resolve_exception(exc["exceptionId"], action=ACTION_DISMISS)


def test_a_rejected_payment_has_no_exception_and_nothing_500s(service, db):
    """Regression (doc 24 §4) — an insufficient-funds REJECTED payment produces no
    exception (the producers fire only for settlement/duplicate/reconciliation), and
    resolving a non-existent exception raises ValueError (→ 404) rather than 500. The
    funds refusal raises (and the saga marks the doc REJECTED) — same pattern as the
    stage-3 insufficient-funds test."""
    with pytest.raises(ValueError, match="Insufficient available balance"):
        _initiate(service, instructed_amount=10_000.01)  # > 10_000 available → REJECTED
    payment = _db_payment(db)
    assert payment["lifecycle"]["currentState"] == lifecycle.REJECTED
    assert _exceptions(db) == []
    with pytest.raises(ValueError, match="not found"):
        service.resolve_exception("EXC-any", action=ACTION_DISMISS)


# --- B1/B2/B4/B5 pinning tests (2026-09-23) ----------------------------------
# These pin the fixes from the stage-9 review audit. Each reproduces the failure scenario
# the audit described and asserts the new invariant holds.


def test_b1_accept_discrepancy_on_reconciliation_discrepancy_flips_the_axis_to_reconciled(
    service, db,
):
    """B1 — ACCEPT_DISCREPANCY on a RECONCILIATION_DISCREPANCY exception must flip
    `lifecycle.reconciliationStatus` to RECONCILED. Without it the post-batch sweep
    (eligibility = currentState in {SETTLED, POSTED} AND reconciliationStatus != RECONCILED)
    re-detects the same mismatch next cycle and `record_exception` opens a FRESH OPEN
    exception — an infinite queue loop where the only legal action (accept) never sticks."""
    from process.exceptions import record_exception

    # A settled external wire (MATCHED) — the reconciliation pass would normally RECONCILE
    # it; instead simulate a discrepancy by stamping one directly, as _stamp_discrepant does.
    from contexts.payment_settlement import settle
    from tests.test_payments_service import FakeConnection
    _initiate_external(service, settlement_outcome="MATCHED")
    settle.complete_due(FakeConnection(db), "leafy_bank_bian", delay_seconds=0)  # deferred completion (36215b1)
    payment = _db_payment(db)
    pid = payment["paymentId"]
    record_exception(
        service._collections(), payment, CATEGORY_RECONCILIATION_DISCREPANCY,
        detail={"discrepancyAmount": 25.0, "discrepancyReason": "leg 2 MISMATCH",
                "expectedAmount": 25000.0, "actualAmount": 24975.0,
                "returnCode": None, "duplicateOf": None},
        source={"stage": SOURCE_STAGE_RECONCILE, "service": SERVICE_LEDGER},
    )
    # mark the payment DISCREPANT on the axis, as the sweep does
    db["payments"].update_one(
        {"paymentId": pid},
        {"$set": {"lifecycle.reconciliationStatus": "DISCREPANT"}},
    )
    exc = _exc(db)
    assert exc["category"] == CATEGORY_RECONCILIATION_DISCREPANCY

    service.resolve_exception(exc["exceptionId"], action=ACTION_ACCEPT_DISCREPANCY,
                              note="Correspondent fee — accepted.")

    payment_after = db["payments"].find_one({"paymentId": pid})
    assert payment_after["lifecycle"]["reconciliationStatus"] == "RECONCILED"
    # the state axis is untouched — SETTLED stays SETTLED (an axis flip, not a transition)
    assert payment_after["lifecycle"]["currentState"] == lifecycle.SETTLED


def test_b2_return_funds_is_idempotent_a_second_resolve_does_not_double_compensate(
    service, db,
):
    """B2 — a double-click / two operators / a crash between the ACID commit and the status
    write must not restore the debtor twice. The first RETURN_FUNDS wins the OPEN claim
    inside its ACID txn; the second finds the exception no longer OPEN and aborts before any
    balance change. Pinning the money invariant: exactly one reversal, debtor restored once."""
    _initiate_external(service, settlement_outcome="EXCEPTION")
    exc = _exc(db)
    payment = _db_payment(db)
    debtor_id = payment["debtor"]["accountId"]
    debtor_held = db["accounts"].find_one({"accountId": debtor_id})["balance"]["current"]

    # first resolve — compensates
    service.resolve_exception(exc["exceptionId"], action=ACTION_RETURN_FUNDS)
    debtor_once = db["accounts"].find_one({"accountId": debtor_id})["balance"]["current"]
    assert debtor_once == debtor_held + payment["amount"]  # restored by the held amount

    # second resolve on the now-RESOLVED exception → 409, no second compensation
    with pytest.raises(ValueError, match="not OPEN"):
        service.resolve_exception(exc["exceptionId"], action=ACTION_RETURN_FUNDS)
    debtor_twice = db["accounts"].find_one({"accountId": debtor_id})["balance"]["current"]
    assert debtor_twice == debtor_once  # unchanged — no double compensation
    # exactly one compensating transactions doc (reversalOf set)
    revs = [t for t in db["transactions"].docs if t.get("reversalOf")]
    assert len(revs) == 1


def test_b2_return_funds_aborts_if_a_reversal_already_exists(service, db):
    """B2 second guard — even if the exception status check somehow let a second caller
    through (e.g. a replay after a crash between the status commit and the response), the
    reversal-existence check inside the ACID txn aborts before re-moving money. Simulated by
    pre-inserting a reversal doc, then flipping the exception back to OPEN and resolving."""
    from contexts.payment_rail import documents
    from datetime import datetime, timezone

    _initiate_external(service, settlement_outcome="EXCEPTION")
    exc = _exc(db)
    payment = _db_payment(db)
    original = db["transactions"].find_one({"paymentId": payment["paymentId"]})
    # pre-insert a compensating doc as if a prior reversal happened
    rev = documents.compensating_transaction_doc(
        original_txn=original,
        debtor_after=db["accounts"].find_one({"accountId": payment["debtor"]["accountId"]}),
        now=datetime.now(timezone.utc),
    )
    db["transactions"].insert_one(rev)
    debtor_before = db["accounts"].find_one(
        {"accountId": payment["debtor"]["accountId"]})["balance"]["current"]

    with pytest.raises(ValueError, match="already exists"):
        service.resolve_exception(exc["exceptionId"], action=ACTION_RETURN_FUNDS)
    # balance untouched — the txn aborted before the $inc
    debtor_after = db["accounts"].find_one(
        {"accountId": payment["debtor"]["accountId"]})["balance"]["current"]
    assert debtor_after == debtor_before


def test_b5_retry_on_delayed_with_delayed_again_writes_a_fresh_occurrence(service, db):
    """B5 — RETRY_SETTLEMENT that lands DELAYED again must leave a FRESH OPEN exception so
    the queue can re-trigger. The old DELAYED occurrence is RESOLVED first (before the
    re-drive), so record_exception inserts a new row rather than collapsing into the old one.
    Without the reorder, the old row would swallow the new occurrence and then be marked
    RESOLVED — payment stuck at IN_PROGRESS with no OPEN exception to retry from."""
    _initiate_external(service, settlement_outcome="DELAYED")
    first = _exc(db)
    assert first["category"] == CATEGORY_SETTLEMENT_DELAYED

    # retry with DELAYED again — the operator re-drives and settlement still doesn't confirm
    service.resolve_exception(
        first["exceptionId"], action=ACTION_RETRY_SETTLEMENT,
        new_settlement_outcome="DELAYED",
    )

    # the first occurrence is RESOLVED
    first_after = db["exceptions"].find_one({"exceptionId": first["exceptionId"]})
    assert first_after["status"] == STATUS_RESOLVED
    # a FRESH OPEN DELAYED occurrence exists (occurrence-per-doc, B2)
    open_delayed = [e for e in _exceptions(db)
                    if e["category"] == CATEGORY_SETTLEMENT_DELAYED and e["status"] == STATUS_OPEN]
    assert len(open_delayed) == 1
    assert open_delayed[0]["exceptionId"] != first["exceptionId"]
    # the payment is still IN_PROGRESS (DELAYED), not stuck without a queue row
    payment_after = db["payments"].find_one({"paymentId": first["paymentId"]})
    assert payment_after["lifecycle"]["currentState"] == lifecycle.IN_PROGRESS


def test_b4_simulated_settlement_outcome_is_persisted_on_the_payment(service, db):
    """B4 — the simulation lever is written to the payment doc at initiation so it survives
    a saga interruption. A wire initiated UNMATCHED that is later resumed (step-up / manual
    review) must NOT reset to MATCHED. Pinning the persisted field + that _context_from_doc
    restores it onto the rebuilt context (asserted indirectly: the doc carries the value)."""
    _initiate_external(service, settlement_outcome="UNMATCHED")
    payment = _db_payment(db)
    assert payment["simulatedSettlementOutcome"] == "UNMATCHED"
    # a MATCHED wire persists MATCHED
    db["payments"].docs.clear()
    db["exceptions"].docs.clear()
    _initiate_external(service, settlement_outcome="MATCHED")
    assert _db_payment(db)["simulatedSettlementOutcome"] == "MATCHED"


def test_b4_resume_restores_the_settlement_outcome_so_unmatched_survives_a_stepup_hold(
    service, db,
):
    """B4 — the resume path (`_context_from_doc`) must restore `settlement_outcome` from the
    persisted doc. Drive an external wire that hits the step-up hold with UNMATCHED selected,
    then resume with a second factor; the settlement run must still land UNMATCHED (fee-deducted
    alias persisted), not reset to MATCHED. This is the live demo-breaker the audit flagged: without
    persist+restore, a held UNMATCHED wire silently became a happy-path SETTLED on resume."""
    from tests.test_payments_service import _ASSERTION
    weak = {"method": "PASSWORD", "factorCount": 1}
    held = _initiate_external(
        service, instructed_amount=5_000.0,
        settlement_outcome="UNMATCHED", authentication=weak,
    )
    assert held["stepUpRequired"] is True
    assert held["simulatedSettlementOutcome"] == "UNMATCHED"
    pid = held["paymentId"]

    service.resume_payment(pid, customer_ref=held["customerId"], authentication=_ASSERTION)

    resumed = db["payments"].find_one({"paymentId": pid})
    # UNMATCHED survived the resume: the fee-deducted statement alias is persisted and the
    # position records the outcome (the bug reset to MATCHED, which carries neither).
    assert resumed["simulatedStatementOutcome"] == "FEE_DEDUCTED"
    pos = db["settlementPositions"].find_one({"paymentId": pid})
    assert pos["outcome"] == "UNMATCHED"


# --- 2026-09-30 fix pass (plan-stage9-fixes.md F4/F5) ----------------------------------

def test_f5_resolve_errors_are_typed_so_the_router_maps_on_type(service, db):
    from process.exceptions import (
        ExceptionActionNotLegal, ExceptionConflict, ExceptionNotFound,
    )
    with pytest.raises(ExceptionNotFound):
        service.resolve_exception("EXC-nonexistent", action=ACTION_DISMISS)
    _, exc = _discrepant_wire(service, db)
    with pytest.raises(ExceptionActionNotLegal):
        service.resolve_exception(exc["exceptionId"], action=ACTION_DISMISS)
    service.resolve_exception(exc["exceptionId"], action=ACTION_ACCEPT_DISCREPANCY)
    with pytest.raises(ExceptionConflict):
        service.resolve_exception(exc["exceptionId"], action=ACTION_ACCEPT_DISCREPANCY)


def test_f4_failed_retry_surfaces_the_original_error_when_reopen_collides(service, db, monkeypatch):
    """A retry whose settle wrote a fresh OPEN occurrence and then raised: the re-open hits
    idx_exception_open_unique. The operator must see the settle error, not a 500 from the
    index collision."""
    from pymongo.errors import DuplicateKeyError

    _initiate_external(service, settlement_outcome="DELAYED")
    exc = _exc(db)

    def boom(*a, **k):
        raise RuntimeError("settle blew up")
    monkeypatch.setattr(service, "settle_payment", boom)

    coll = service.db["exceptions"]
    real_update = coll.update_one

    def colliding_update(filt, update, *a, **k):
        if update.get("$set", {}).get("status") == STATUS_OPEN:
            raise DuplicateKeyError("E11000 idx_exception_open_unique")
        return real_update(filt, update, *a, **k)
    monkeypatch.setattr(coll, "update_one", colliding_update)

    with pytest.raises(RuntimeError, match="settle blew up"):
        service.resolve_exception(
            exc["exceptionId"], action=ACTION_RETRY_SETTLEMENT, new_settlement_outcome="MATCHED",
        )
