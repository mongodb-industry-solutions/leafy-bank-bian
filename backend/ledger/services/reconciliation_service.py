"""Reconciliation service — verify subledger ↔ journal integrity.

Implements the reconciliation expression:
  Σ subLedgerEntries WHERE controlAccountCode = X  ==  Σ journalEntries.entries[].amount WHERE accountCode = X

Both sums are signed (DR positive, CR negative) using the DR+/CR- convention throughout the
pipeline.

The subledger side is filtered to *journaled* rows only (status == POSTED AND journalEntryId
set). subLedgerEntries are born POSTED at projection (before the batch journal write, per design
Step 4.5), so filtering on status alone would count rows whose journal does not yet exist and
report a false mismatch during the projection→journal lag. journalEntryId is stamped at journal
write, so both sides move together and the check is trustworthy at any time. Note: this means a
row permanently stuck in projection (never journaled) is NOT flagged by this sum — that is a
separate "stuck rows" metric (see DN-2 in schema-bian-deviation.md), not a DR/CR imbalance.

Run period-scoped (period_code kwarg) for continuous monitoring; bounds the aggregation to
monthly volume via idx_period_code + idx_entries_account_code.
Run all-time (period_code=None) only for end-of-period audits — O(all-history).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from database.connection import MongoDBConnection
from services.exceptions_service import (
    ACTION_RECHECK,
    CATEGORY_RECONCILIATION_DISCREPANCY,
    CATEGORY_RECONCILIATION_MISSING,
    SERVICE_LEDGER,
    STATUS_OPEN,
    STATUS_RESOLVED,
    SOURCE_STAGE_RECONCILE,
    record_exception,
)

logger = logging.getLogger(__name__)


# --- Stage 8: three-way reconciliation (doc 22) --------------------------------
#
# The pre-existing code below this block (`reconcile_account`, `reconcile_all_accounts`, …)
# checks **internal GL integrity** — Σ subLedgerEntries == Σ journalEntries per GL account —
# which is the pre-batch *gate*. Doina's stage 8 (L640-644) is a different thing: a **three-way
# tie-out per payment** that asks whether the instruction, the rail, and the GL all agree. It runs
# *post*-batch and stamps `RECONCILED` on the payment when all three legs pass. The two share the
# signed-amount convention (DR+/CR-) but nothing else.
#
# The three legs (doc 22 B2):
#   1. Payment ↔ Rail         — did the rail process what we sent?
#   2. Rail ↔ Settlement       — did cash actually move (settlementPositions vs the settlement
#                                ledgerEvent)?
#   3. Settlement ↔ GL         — does the GL agree with external settlement (journal posted +
#                                the clearing account nets to zero)?
# Legs 1 and 2 are NOT_APPLICABLE for an internal book transfer (no rail artifact, no settlement
# run — doc 21 B3, Q39); leg 3 then reduces to "the principal journal posted".

# Reconciliation status on `payments.lifecycle.reconciliationStatus` (Q57). PENDING is a
# *transient* check result, never a persisted status — a payment that is not yet checkable stays
# at null/UNRECONCILED and is retried next batch.
RECONCILED = "RECONCILED"
DISCREPANT = "DISCREPANT"
PENDING = "PENDING"

# Per-leg results.
LEG_MATCH = "MATCH"
LEG_MISMATCH = "MISMATCH"
LEG_NOT_APPLICABLE = "NOT_APPLICABLE"
LEG_PENDING = "PENDING"
REASON_AWAITING_STATEMENT = "AWAITING_STATEMENT"
# Plan A4 D2 — POST_ADJUSTMENT was approved but the `-ADJ` event has not posted yet.
REASON_AWAITING_ADJUSTMENT = "AWAITING_ADJUSTMENT"

LEG_PAYMENT_RAIL = "PAYMENT_RAIL"
LEG_RAIL_SETTLEMENT = "RAIL_SETTLEMENT"
LEG_SETTLEMENT_GL = "SETTLEMENT_GL"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _majors_to_minors(amount) -> Optional[int]:
    """Convert a major-unit amount (float/int, what `payments.amount` stores) to signed minor
    units (int) for comparison against `ledgerEvents.debitLeg.amount` (minors).

    Returns None for a None/missing input so the caller can raise a PENDING leg rather than a
    false zero. Rounds to the nearest minor unit — the repo stores majors as float dollars.
    """
    if amount is None:
        return None
    return int(round(float(amount) * 100))


@dataclass(frozen=True)
class LegResult:
    leg: str
    result: str
    left_amount: Optional[int] = None   # minor units, signed where applicable
    right_amount: Optional[int] = None  # minor units
    detail: str = ""
    # Machine-readable cause for a PENDING leg (plan A3), so callers never string-match
    # `detail`. Only AWAITING_STATEMENT is set today.
    reason: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "leg": self.leg,
            "result": self.result,
            "leftAmount": self.left_amount,
            "rightAmount": self.right_amount,
            "detail": self.detail,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReconciliationCheck:
    payment_id: str
    legs: list[LegResult] = field(default_factory=list)
    overall: str = PENDING            # RECONCILED | DISCREPANT | PENDING
    journal_entry_id: Optional[str] = None
    settlement_position_id: Optional[str] = None
    # settlementPositions.expectedWindow.by — when the statement line is due (plan A3).
    expected_window_by: Optional[datetime] = None
    checked_at: datetime = field(default_factory=_now_utc)

    @property
    def awaiting_statement(self) -> bool:
        return any(lg.reason == REASON_AWAITING_STATEMENT for lg in self.legs)

    @property
    def is_reconciled(self) -> bool:
        return self.overall == RECONCILED

    def as_dict(self) -> dict:
        return {
            "paymentId": self.payment_id,
            "legs": [lg.as_dict() for lg in self.legs],
            "overallResult": self.overall,
            "journalEntryId": self.journal_entry_id,
            "settlementPositionId": self.settlement_position_id,
            "checkedAt": self.checked_at,
        }


def _signed_leg_amount(leg: Optional[dict]) -> int:
    """Signed minor-unit amount for a ledgerEvent leg, DR+/CR- (the pipeline convention)."""
    if not leg:
        return 0
    amt = int(leg.get("amount") or 0)
    # The side is inferred from which named leg it is: debitLeg is DR (+), creditLeg is CR (-).
    # `ledgerEvents` stores one debitLeg and one creditLeg (decisions.md 2026-06-18), not a
    # `side` field on each, so the caller passes the leg plus its sign.
    return amt


def _clearing_account_net(events: list[dict], clearing_code: str) -> int:
    """Σ signed amounts for `glAccountCode == clearing_code` across the payment's ledgerEvents.

    DR legs are positive, CR legs negative (the DR+/CR- convention `reconcile_account` uses).
    A settled external wire credits the clearing account in the principal event and debits it in
    the settlement event, so the net is zero — which is R10's *"holding account cleared"* proof.
    """
    total = 0
    for evt in events:
        debit = evt.get("debitLeg") or {}
        credit = evt.get("creditLeg") or {}
        if debit.get("glAccountCode") == clearing_code:
            total += int(debit.get("amount") or 0)
        if credit.get("glAccountCode") == clearing_code:
            total -= int(credit.get("amount") or 0)
    return total


def compute_reconciliation(
    payment_id: str,
    connection: MongoDBConnection,
    db_name: str,
) -> Optional[ReconciliationCheck]:
    """Doina's three-way reconciliation for one payment (doc 22 B2). Pure reads, no writes.

    Returns None if the payment does not exist. A payment whose downstream artifacts are not all
    present yet (e.g. the settlement journal has not posted) returns a check with
    `overall = PENDING` — the post-batch pass retries it next cycle rather than flagging a
    discrepancy.
    """
    payments = connection.get_collection(db_name, "payments")
    payment = payments.find_one({"paymentId": payment_id}, {"_id": 0})
    if payment is None:
        return None

    rail = payment.get("rail")
    is_internal = rail == "INTERNAL"
    payment_amount_minors = _majors_to_minors(payment.get("amount"))
    legs: list[LegResult] = []

    # --- the payment's ledgerEvents (principal / fee / settlement) ---------------
    le_coll = connection.get_collection(db_name, "ledgerEvents")
    all_events = list(le_coll.find(
        {"idempotencyKey": {"$in": [
            payment_id,
            f"{payment_id}-FEE",
            f"{payment_id}-SETTLEMENT",
            f"{payment_id}-ADJ",
        ]}},
        {"_id": 0},
    ))
    principal_event = next(
        (e for e in all_events if e.get("idempotencyKey") == payment_id), None
    )
    settlement_event = next(
        (e for e in all_events if e.get("idempotencyKey") == f"{payment_id}-SETTLEMENT"), None
    )
    adjustment_event = next(
        (e for e in all_events if e.get("idempotencyKey") == f"{payment_id}-ADJ"), None
    )

    # --- Leg 1: Payment ↔ Rail ---------------------------------------------------
    # N/A for an internal transfer (no execution artifact by design, Q39).
    if is_internal:
        legs.append(LegResult(LEG_PAYMENT_RAIL, LEG_NOT_APPLICABLE,
                              detail="Book transfer — no rail artifact (Q39)."))
    elif payment.get("direction") == "INBOUND":
        # FR-8.IN1 — an inbound payment has NO execution record by design (her L944:
        # `paymentExecutions` tracks outbound rail submissions only), so leg 1 compares
        # what inbound DOES have: the received pacs.008 (stage 1) and the transmitted
        # pacs.002 status report (stage 5) against the posted ledger entry (the principal
        # event's clearing debit — the amount actually credited).
        #
        # Without this branch every inbound payment sat at leg-1 PENDING forever
        # ("No paymentExecutions doc yet") — rescanned each batch, never RECONCLED.
        # The message-side constants are mirrored from the transactions service's
        # `inbound_documents` / `pacs002` (no cross-service import; same discipline as
        # the exceptions stub's twin).
        pm_coll = connection.get_collection(db_name, "paymentMessages")
        received = pm_coll.find_one({
            "paymentId": payment_id, "direction": "INBOUND",
            "purpose": "CREDIT_TRANSFER",
        })
        response = pm_coll.find_one({
            "paymentId": payment_id, "purpose": "STATUS_RESPONSE",
        })
        if received is None:
            legs.append(LegResult(
                LEG_PAYMENT_RAIL, LEG_PENDING,
                detail="No stored inbound pacs.008 yet — the gateway has not recorded the message."))
        elif response is None:
            legs.append(LegResult(
                LEG_PAYMENT_RAIL, LEG_PENDING,
                detail="No transmitted pacs.002 yet — stage 5 has not confirmed to the sender."))
        elif response.get("statusCode") != "ACCP":
            legs.append(LegResult(
                LEG_PAYMENT_RAIL, LEG_MISMATCH,
                left_amount=payment_amount_minors,
                detail=f"The transmitted pacs.002 says {response.get('statusCode')}, not ACCP — "
                       "the sender was not told this payment would be applied."))
        elif principal_event is None:
            legs.append(LegResult(
                LEG_PAYMENT_RAIL, LEG_PENDING,
                detail="The credit has not posted yet (no principal ledgerEvent)."))
        else:
            posted_minors = principal_event.get("debitLeg", {}).get("amount")
            if payment_amount_minors is None or posted_minors is None:
                legs.append(LegResult(
                    LEG_PAYMENT_RAIL, LEG_PENDING,
                    detail="Amount missing on the payment or the posted credit leg."))
            elif payment_amount_minors == posted_minors:
                legs.append(LegResult(
                    LEG_PAYMENT_RAIL, LEG_MATCH,
                    left_amount=payment_amount_minors,
                    right_amount=posted_minors,
                    detail="Inbound message received and pacs.002 (ACCP) transmitted; "
                           "credited amount == posted clearing debit."))
            else:
                legs.append(LegResult(
                    LEG_PAYMENT_RAIL, LEG_MISMATCH,
                    left_amount=payment_amount_minors,
                    right_amount=posted_minors,
                    detail="Credited amount != posted clearing debit."))
    else:
        pe_coll = connection.get_collection(db_name, "paymentExecutions")
        executions = list(pe_coll.find({"paymentId": payment_id}, {"_id": 0}).sort("attempt", 1))
        if not executions:
            legs.append(LegResult(LEG_PAYMENT_RAIL, LEG_PENDING,
                                  detail="No paymentExecutions doc yet — rail has not recorded the attempt."))
        else:
            # The last attempt is the current one (append-only, doc 19 B3).
            execution = executions[-1]
            rail_amt_minors = _majors_to_minors(execution.get("amount"))
            rail_status = execution.get("railStatus") or {}
            acknowledged = bool(rail_status.get("code"))
            if payment_amount_minors is None or rail_amt_minors is None:
                legs.append(LegResult(LEG_PAYMENT_RAIL, LEG_PENDING,
                                      detail="Amount missing on the payment or the execution."))
            elif payment_amount_minors == rail_amt_minors and acknowledged:
                legs.append(LegResult(LEG_PAYMENT_RAIL, LEG_MATCH,
                                      left_amount=payment_amount_minors,
                                      right_amount=rail_amt_minors,
                                      detail=f"Rail acknowledged {rail_status.get('code')}; instruction amount == execution amount."))
            elif payment_amount_minors != rail_amt_minors:
                legs.append(LegResult(LEG_PAYMENT_RAIL, LEG_MISMATCH,
                                      left_amount=payment_amount_minors,
                                      right_amount=rail_amt_minors,
                                      detail="Instruction amount != execution amount."))
            else:
                legs.append(LegResult(LEG_PAYMENT_RAIL, LEG_PENDING,
                                      detail="Rail has not acknowledged the execution (railStatus.code is null)."))

    # --- Leg 2: Rail ↔ Settlement account ----------------------------------------
    # Compares the external settlement record (settlementPositions, stage 7) against the
    # settlement ledgerEvent (what we posted to the clearing/settlement accounts). N/A for an
    # internal transfer (no settlement run). This leg IS the mirror account (doc 22 B3 / Q58):
    # expected (grossAmount) vs actual (the posted leg).
    sp_coll = connection.get_collection(db_name, "settlementPositions")
    positions = list(sp_coll.find({"paymentId": payment_id}, {"_id": 0}).sort("createdAt", 1))
    position = positions[-1] if positions else None

    if is_internal:
        legs.append(LegResult(LEG_RAIL_SETTLEMENT, LEG_NOT_APPLICABLE,
                              detail="Book transfer — no external settlement run."))
    elif position is None:
        legs.append(LegResult(LEG_RAIL_SETTLEMENT, LEG_PENDING,
                              detail="No settlementPositions doc yet — stage 7 has not settled."))
    elif settlement_event is None:
        legs.append(LegResult(LEG_RAIL_SETTLEMENT, LEG_PENDING,
                              detail="Settlement response received but the settlement ledgerEvent has not been derived yet (awaiting CDC)."))
    else:
        # FR-7.4: compare the stored expected (clearing amount) vs the posted settlement
        # debit leg. Fall back to grossAmount for positions written before the FR-7.4 fix
        # (expectedAmount absent) — safe because those are all USD-USD wires where the two
        # are equal.
        expected = position.get("expectedAmount", position.get("grossAmount"))
        expected_minors = _majors_to_minors(expected)
        posted_minors = _signed_leg_amount(settlement_event.get("debitLeg"))
        settled = position.get("settlementStatus") == "SETTLED"
        # Sep 17 L1264-1270: the rail can settle short ($24,975) while the GL settlement
        # posting carries the full $25,000. Compare what actually settled, when recorded.
        actual_minors = _majors_to_minors(position.get("actualAmount"))
        # Plan A4 D2 — an approved DEBT correction (Dr 5214 / Cr nostro) books the
        # correspondent's charge, so the nostro moved by posted − adjustment, which is what
        # the statement shows. Reconciliation closes the leg itself; the action never does.
        if adjustment_event is not None:
            posted_minors -= _signed_leg_amount(adjustment_event.get("creditLeg"))
        if (settled and adjustment_event is None and position.get("adjustmentPending")
                and actual_minors is not None and actual_minors != posted_minors):
            # Approved but not yet posted. Without this the sweep would read MISMATCH and
            # open a fresh DISCREPANCY behind the one POST_ADJUSTMENT just closed (B1 class).
            legs.append(LegResult(LEG_RAIL_SETTLEMENT, LEG_PENDING,
                                  left_amount=posted_minors,
                                  right_amount=actual_minors,
                                  detail="Correspondent-charge adjustment approved; awaiting its ledger event.",
                                  reason=REASON_AWAITING_ADJUSTMENT))
        elif settled and actual_minors is not None and actual_minors != posted_minors:
            legs.append(LegResult(LEG_RAIL_SETTLEMENT, LEG_MISMATCH,
                                  left_amount=posted_minors,
                                  right_amount=actual_minors,
                                  detail=(f"Settlement position {position.get('actualAmount')} "
                                          f"!= GL settlement posting {expected} — "
                                          "rail settled short.")))
        elif (settled and actual_minors is None
              and payment.get("direction") != "INBOUND"):
            # Reconciliation plan A2 — an outbound wire's actual amount comes only from the
            # correspondent's camt.053 line (`statement_matching`). Until one is matched the
            # external record is missing, so the leg waits. Inbound positions carry their
            # actual at arrival and never appear on a statement, so they skip this.
            legs.append(LegResult(LEG_RAIL_SETTLEMENT, LEG_PENDING,
                                  left_amount=posted_minors,
                                  detail="Awaiting the correspondent statement line for this settlement.",
                                  reason=REASON_AWAITING_STATEMENT))
        elif settled and adjustment_event is not None:
            # Reached only when actual == posted − adjustment (the mismatch branch above
            # took every other case), so the correction ties the nostro out.
            legs.append(LegResult(LEG_RAIL_SETTLEMENT, LEG_MATCH,
                                  left_amount=posted_minors,
                                  right_amount=actual_minors,
                                  detail=(f"Statement actual {position.get('actualAmount')} == settlement "
                                          "posting net of the approved correspondent-charge adjustment.")))
        elif expected_minors is None:
            legs.append(LegResult(LEG_RAIL_SETTLEMENT, LEG_PENDING,
                                  detail="settlementPositions.expectedAmount is missing."))
        elif expected_minors == posted_minors and settled:
            legs.append(LegResult(LEG_RAIL_SETTLEMENT, LEG_MATCH,
                                  left_amount=expected_minors,
                                  right_amount=posted_minors,
                                  detail=f"Expected (mirror) {expected} == posted settlement leg; settlementStatus SETTLED."))
        elif expected_minors != posted_minors:
            legs.append(LegResult(LEG_RAIL_SETTLEMENT, LEG_MISMATCH,
                                  left_amount=expected_minors,
                                  right_amount=posted_minors,
                                  detail="Settlement position expectedAmount != settlement ledgerEvent leg amount."))
        else:
            legs.append(LegResult(LEG_RAIL_SETTLEMENT, LEG_MISMATCH,
                                  left_amount=expected_minors,
                                  right_amount=posted_minors,
                                  detail=f"Amounts match but settlementStatus is {position.get('settlementStatus')}, not SETTLED."))

    # --- Leg 3: Settlement account ↔ GL ------------------------------------------
    # External: the settlement journal posted AND the clearing account nets to zero across the
    # payment's events (the in-flight position is cleared — R10). Internal: the principal
    # journal posted (the only journal an internal transfer produces).
    if is_internal:
        if principal_event is None:
            legs.append(LegResult(LEG_SETTLEMENT_GL, LEG_PENDING,
                                  detail="No principal ledgerEvent yet — awaiting CDC from the transactions insert."))
        elif principal_event.get("postingStatus") != "POSTED":
            legs.append(LegResult(LEG_SETTLEMENT_GL, LEG_PENDING,
                                  detail="Principal ledgerEvent not yet posted — awaiting the GL batch."))
        else:
            legs.append(LegResult(LEG_SETTLEMENT_GL, LEG_MATCH,
                                  detail="Principal journal posted (book transfer — no clearing account to clear)."))
    else:
        if settlement_event is None:
            legs.append(LegResult(LEG_SETTLEMENT_GL, LEG_PENDING,
                                  detail="No settlement ledgerEvent yet — awaiting CDC from the settlement transition."))
        elif settlement_event.get("postingStatus") != "POSTED":
            legs.append(LegResult(LEG_SETTLEMENT_GL, LEG_PENDING,
                                  detail="Settlement ledgerEvent not yet posted — awaiting the GL batch."))
        else:
            clearing_code = (position or {}).get("clearingAccountCode")
            if not clearing_code:
                legs.append(LegResult(LEG_SETTLEMENT_GL, LEG_MISMATCH,
                                      detail="Settlement posted but settlementPositions.clearingAccountCode is missing — cannot verify the clearing account nets to zero."))
            else:
                net = _clearing_account_net(all_events, clearing_code)
                if net == 0:
                    legs.append(LegResult(LEG_SETTLEMENT_GL, LEG_MATCH,
                                          left_amount=0,
                                          right_amount=net,
                                          detail=f"Settlement journal posted; clearing account {clearing_code} nets to zero across the payment's events."))
                else:
                    legs.append(LegResult(LEG_SETTLEMENT_GL, LEG_MISMATCH,
                                          left_amount=0,
                                          right_amount=net,
                                          detail=f"Settlement journal posted but clearing account {clearing_code} nets to {net}, not zero — in-flight position not cleared."))

    # --- overall -----------------------------------------------------------------
    results = [lg.result for lg in legs]
    if LEG_MISMATCH in results:
        overall = DISCREPANT
    elif LEG_PENDING in results:
        overall = PENDING
    else:
        overall = RECONCILED  # all MATCH or NOT_APPLICABLE

    # Evidence pointers for the reconciliationItems doc and the payments refs.
    journal_entry_id = None
    if settlement_event is not None:
        # DR-7.3: reference the settlement-leg journal entry from `settlementPositions`
        # (written back by the ledger when the settlement event posts). Fall back to the
        # settlement event's subLedgerEntries for pre-DR-7.3 records that predate the write-back.
        journal_entry_id = (position or {}).get("journalEntryId")
        if not journal_entry_id:
            sl_coll = connection.get_collection(db_name, "subLedgerEntries")
            sl = sl_coll.find_one(
                {"sourceReference.sourceId": settlement_event.get("eventId")},
                {"_id": 0, "journalEntryId": 1},
            )
            journal_entry_id = (sl or {}).get("journalEntryId") or None
    elif principal_event is not None:
        journal_entry_id = (payment.get("refs") or {}).get("journalEntryId")

    settlement_position_id = (position or {}).get("settlementPositionId")

    return ReconciliationCheck(
        payment_id=payment_id,
        legs=legs,
        overall=overall,
        journal_entry_id=journal_entry_id,
        settlement_position_id=settlement_position_id,
        expected_window_by=((position or {}).get("expectedWindow") or {}).get("by"),
    )


# --- the post-batch pass (doc 22 B5 / step 3) ---------------------------------
#
# Runs at the end of `gl_batch.run_one_cycle`, AFTER `run_batch` posts journals. Sweeps every
# payment that is SETTLED (external) or POSTED (internal) and not yet RECONCILED, computes the
# three legs, and either:
#   - stamps RECONCILED + writes reconciliationItems, when all legs MATCH / NOT_APPLICABLE; or
#   - stamps DISCREPANT + writes reconciliationItems, when any leg MISMATCH; or
#   - does nothing, when any leg is PENDING (retried next batch — the settlement journal may
#     not have posted yet).
#
# The RECONCILED write mirrors `posting_writeback_service._write_back`'s POSTED write: a direct
# `$set` of `currentState`/`status`/`reconciliationStatus` + a `$push` of one `lifecycle.events`
# entry, guarded on `currentState` in the query so a FAILED/RETURNED payment can never reach it.
# It does NOT import the transactions service's `lifecycle` module (mirror-drift, defects.md
# 2026-06-18); the event shape is mirrored and asserted by
# `test_the_reconciled_event_matches_the_state_machines_own_shape`.

_RECONCILED_STATE = "RECONCILED"
_ACTOR = "ledger-service"
_ACTOR_TYPE = "SERVICE"
_SOURCE_SYSTEM = "ledger-service"
_ELIGIBLE_STATES = ("SETTLED", "POSTED")


def _upsert_reconciliation_item(ri_coll, check: ReconciliationCheck) -> dict:
    """Write the check onto the payment's one open `reconciliationItems` doc (B4 shape).

    Plan A3: a DISCREPANT or overdue payment is re-checked every batch, and inserting each
    time grew one item per payment per cycle. The open item (any result but RECONCILED) is
    updated in place, so `reconciliationItemId` stays stable across re-checks; a RECONCILED
    item is history and never rewritten. Not unique-indexed — a `$ne` filter cannot be a
    unique key — so a sweep racing `POST /pipeline/reconcile/{id}` can at worst leave a second
    open item, never lose one.
    """
    from bson import ObjectId
    from pymongo import ReturnDocument
    from shared.refs import PREFIX_RECONCILIATION_ITEM, derive_ref
    oid = ObjectId()
    return ri_coll.find_one_and_update(
        {"paymentId": check.payment_id, "overallResult": {"$ne": RECONCILED}},
        {
            "$set": {
                "legs": [lg.as_dict() for lg in check.legs],
                "overallResult": check.overall,
                "journalEntryId": check.journal_entry_id,
                "settlementPositionId": check.settlement_position_id,
                "checkedAt": check.checked_at,
                "sourceSystem": _SOURCE_SYSTEM,
            },
            "$setOnInsert": {
                "_id": oid,
                "reconciliationItemId": derive_ref(PREFIX_RECONCILIATION_ITEM, oid),
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


def _stamp_reconciled(payments, payment_id: str, check: ReconciliationCheck, ri_coll) -> None:
    """Advance a SETTLED/POSTED payment to RECONCILED and record the reconciliation item.

    Guarded on `currentState` in the query, so a concurrent transition to a terminal state loses
    rather than double-transitioning. Idempotent: a payment already RECONCILED is excluded by the
    sweep filter, so this only fires once per payment.
    """
    now = _now_utc()
    item = _upsert_reconciliation_item(ri_coll, check)

    payments.update_one(
        {"paymentId": payment_id, "lifecycle.currentState": {"$in": list(_ELIGIBLE_STATES)}},
        {
            "$set": {
                "lifecycle.currentState": _RECONCILED_STATE,
                "lifecycle.stateEnteredAt": now,
                "lifecycle.reconciliationStatus": RECONCILED,
                "status": _RECONCILED_STATE,
                "updatedAt": now,
                "refs.reconciliationItemId": item["reconciliationItemId"],
                "refs.settlementPositionId": check.settlement_position_id,
            },
            "$push": {"lifecycle.events": {
                "state": _RECONCILED_STATE,
                "at": now,
                "actor": _ACTOR,
                "actorType": _ACTOR_TYPE,
                "reason": f"Three-way reconciliation passed — {item['reconciliationItemId']}",
            }},
        },
    )


def _stamp_discrepant(payments, payment_id: str, check: ReconciliationCheck, ri_coll, exc_coll) -> None:
    """Record a discrepancy without advancing state. The payment stays at SETTLED/POSTED and
    feeds stage 9's exception queue (doc 24 B3 site 4)."""
    item = _upsert_reconciliation_item(ri_coll, check)

    payments.update_one(
        {"paymentId": payment_id},
        {
            "$set": {
                "lifecycle.reconciliationStatus": DISCREPANT,
                "updatedAt": _now_utc(),
                "refs.reconciliationItemId": item["reconciliationItemId"],
            },
        },
    )

    # Stage 9 — queue the discrepancy. The mismatched leg's leftAmount/rightAmount (minor
    # units, the pipeline convention) become the exception detail in MAJOR units, so the
    # Operations queue row shows the $25 short-pay exactly as the tie-out saw it. Idempotent
    # on (paymentId, category, OPEN) — a DISCREPANT payment re-checked next batch does not
    # double-insert (B3).
    mismatch = next((lg for lg in check.legs if lg.result == LEG_MISMATCH), None)
    if mismatch is not None:
        expected_min = int(mismatch.left_amount or 0)
        actual_min = int(mismatch.right_amount or 0)
        detail = {
            "discrepancyAmount": (expected_min - actual_min) / 100.0,
            "discrepancyReason": mismatch.detail or f"Leg {mismatch.leg} MISMATCH",
            "expectedAmount": expected_min / 100.0,
            "actualAmount": actual_min / 100.0,
            "returnCode": None,
            "duplicateOf": None,
        }
    else:
        detail = {
            "discrepancyAmount": None,
            "discrepancyReason": "DISCREPANT (no single mismatched leg)",
            "expectedAmount": None,
            "actualAmount": None,
            "returnCode": None,
            "duplicateOf": None,
        }
    record_exception(
        exc_coll, payment_id, CATEGORY_RECONCILIATION_DISCREPANCY, detail,
        source={"stage": SOURCE_STAGE_RECONCILE, "service": SERVICE_LEDGER},
    )


def _stamp_missing(check: ReconciliationCheck, ri_coll, exc_coll) -> None:
    """Plan A3 watchdog — the wire settled but the correspondent's statement line did not
    arrive by `expectedWindow.by` (Sep 17's flagship agent case). Records the overdue check
    and queues `RECONCILIATION_MISSING`; the payment's `reconciliationStatus` is untouched —
    missing is "not yet", not DISCREPANT, so the sweep keeps re-checking and a late line
    still reconciles it."""
    _upsert_reconciliation_item(ri_coll, check)
    leg = next((lg for lg in check.legs if lg.reason == REASON_AWAITING_STATEMENT), None)
    expected_min = leg.left_amount if leg is not None else None
    by = check.expected_window_by
    record_exception(
        exc_coll, check.payment_id, CATEGORY_RECONCILIATION_MISSING,
        {
            "discrepancyAmount": None,
            "discrepancyReason": f"No correspondent statement line by {by.isoformat() if by else 'the expected window'}.",
            "expectedAmount": expected_min / 100.0 if expected_min is not None else None,
            "actualAmount": None,
            "returnCode": None,
            "duplicateOf": None,
            "expectedWindowBy": by,
            "settlementPositionId": check.settlement_position_id,
        },
        source={"stage": SOURCE_STAGE_RECONCILE, "service": SERVICE_LEDGER},
    )


def _resolve_missing(exc_coll, payment_id: str, now: datetime) -> None:
    """Plan A3 D2 — the statement line has arrived, so an open MISSING is no longer true.
    Conditional on OPEN, so an operator who resolved it first wins cleanly."""
    result = exc_coll.update_one(
        {"paymentId": payment_id, "category": CATEGORY_RECONCILIATION_MISSING, "status": STATUS_OPEN},
        {"$set": {
            "status": STATUS_RESOLVED,
            "resolution": {"action": ACTION_RECHECK, "by": _ACTOR, "at": now,
                           "note": "Correspondent statement line arrived."},
            "updatedAt": now,
        }},
    )
    if result.matched_count:
        logger.info("reconciliation: RECONCILIATION_MISSING auto-resolved for %s", payment_id)


def _is_overdue(check: ReconciliationCheck, now: datetime) -> bool:
    by = check.expected_window_by
    if not check.awaiting_statement or by is None:
        return False  # pre-A2 positions carry no window — never raised
    if by.tzinfo is None:  # PyMongo returns naive UTC datetimes by default
        by = by.replace(tzinfo=timezone.utc)
    return by < now


# Outcomes of one `reconcile_payment` call.
OUTCOME_MISSING = "MISSING"


def reconcile_payment(payment_id: str, connection: MongoDBConnection, db_name: str,
                      now: Optional[datetime] = None) -> tuple[Optional[ReconciliationCheck], Optional[str]]:
    """Check one payment and stamp the result. Returns (check, outcome), where outcome is
    RECONCILED | DISCREPANT | PENDING | MISSING; (None, None) if the payment does not exist.

    Shared by the post-batch sweep and `POST /pipeline/reconcile/{paymentId}` (plan A3, the
    spec's "re-run reconciliation" step). The caller decides eligibility.
    """
    now = now or _now_utc()
    payments = connection.get_collection(db_name, "payments")
    ri_coll = connection.get_collection(db_name, "reconciliationItems")
    exc_coll = connection.get_collection(db_name, "exceptions")

    check = compute_reconciliation(payment_id, connection, db_name)
    if check is None:
        return None, None
    if not check.awaiting_statement:
        _resolve_missing(exc_coll, payment_id, now)

    if check.overall == RECONCILED:
        _stamp_reconciled(payments, payment_id, check, ri_coll)
        logger.info("reconciliation RECONCILED paymentId=%s", payment_id)
        return check, RECONCILED
    if check.overall == DISCREPANT:
        _stamp_discrepant(payments, payment_id, check, ri_coll, exc_coll)
        logger.warning(
            "reconciliation DISCREPANT paymentId=%s — legs: %s",
            payment_id, [lg.result for lg in check.legs],
        )
        return check, DISCREPANT
    if _is_overdue(check, now):
        _stamp_missing(check, ri_coll, exc_coll)
        logger.warning("reconciliation MISSING paymentId=%s — statement line overdue", payment_id)
        return check, OUTCOME_MISSING
    return check, PENDING


def is_eligible(payment: dict) -> bool:
    """SETTLED (external) or POSTED (internal), and not yet RECONCILED."""
    lifecycle = payment.get("lifecycle") or {}
    return (lifecycle.get("currentState") in _ELIGIBLE_STATES
            and lifecycle.get("reconciliationStatus") != RECONCILED)


def reconcile_settled_payments(connection: MongoDBConnection, db_name: str,
                               now: Optional[datetime] = None) -> dict:
    """Post-batch: reconcile every eligible payment. Returns a counts dict.

    Eligible = `currentState` in {SETTLED, POSTED} AND `reconciliationStatus != RECONCILED`.
    A payment whose legs are not all checkable yet (PENDING) is left for the next cycle — this
    is why RECONCILED arrives one batch after the settlement journal posts, like POSTED. A
    PENDING outbound wire past its statement window is raised as MISSING (plan A3).
    """
    now = now or _now_utc()
    payments = connection.get_collection(db_name, "payments")
    eligible = list(payments.find(
        {
            "lifecycle.currentState": {"$in": list(_ELIGIBLE_STATES)},
            "lifecycle.reconciliationStatus": {"$ne": RECONCILED},
        },
        {"_id": 0, "paymentId": 1},
    ))

    counts = {RECONCILED: 0, DISCREPANT: 0, PENDING: 0, OUTCOME_MISSING: 0}
    for doc in eligible:
        payment_id = doc.get("paymentId")
        if not payment_id:
            continue
        _, outcome = reconcile_payment(payment_id, connection, db_name, now=now)
        if outcome is not None:
            counts[outcome] += 1

    if eligible:
        logger.info(
            "post-batch reconciliation: %d reconciled, %d discrepant, %d pending (%d missing) of %d eligible",
            counts[RECONCILED], counts[DISCREPANT], counts[PENDING] + counts[OUTCOME_MISSING],
            counts[OUTCOME_MISSING], len(eligible),
        )
    # `pending` keeps its old meaning (not yet reconcilable) and includes the overdue ones.
    return {"reconciled": counts[RECONCILED], "discrepant": counts[DISCREPANT],
            "pending": counts[PENDING] + counts[OUTCOME_MISSING],
            "missing": counts[OUTCOME_MISSING], "eligible": len(eligible)}


def signed_amount(amount: int, side: str) -> int:
    """Apply DR+/CR- sign convention to an unsigned minor-unit amount."""
    return amount if side == "DEBIT" else -amount


@dataclass(frozen=True)
class ReconciliationResult:
    account_code: str
    period_code: Optional[str]   # None = all-time
    subledger_sum: int           # Σ SL signed (minor units, journaled rows only)
    journal_sum: int             # Σ JNL entries signed (minor units)
    checked_at: datetime

    @property
    def subledger_journal_match(self) -> bool:
        return self.subledger_sum == self.journal_sum

    @property
    def is_reconciled(self) -> bool:
        return self.subledger_journal_match


def _subledger_signed_sum(sl_coll, account_code: str, period_code: Optional[str]) -> int:
    """Σ subLedgerEntries.amount (signed, journaled only) for the given account + optional period."""
    match: dict = {
        "controlAccountCode": account_code,
        "status": "POSTED",
        "journalEntryId": {"$ne": ""},
    }
    if period_code:
        match["periodCode"] = period_code

    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": None,
            "signedSum": {"$sum": {"$cond": [
                {"$eq": ["$side", "DEBIT"]},
                "$amount",
                {"$multiply": [-1, "$amount"]},
            ]}},
        }},
    ]
    result = list(sl_coll.aggregate(pipeline))
    return int(result[0]["signedSum"]) if result else 0


def _journal_signed_sum(jnl_coll, account_code: str, period_code: Optional[str]) -> int:
    """Σ journalEntries.entries[].amount (signed) for the given accountCode + optional period.

    With period_code: hits idx_period_code first (document-level), then
    idx_entries_account_code on the unwound entries — aggregation is bounded to monthly volume.

    Without period_code: hits idx_entries_account_code only — O(all-history). Use for
    end-of-period audits, not continuous monitoring.
    """
    pipeline: list[dict] = []
    if period_code:
        pipeline.append({"$match": {"periodCode": period_code}})
    pipeline += [
        {"$unwind": "$entries"},
        {"$match": {"entries.accountCode": account_code}},
        {"$group": {
            "_id": None,
            "signedSum": {"$sum": {"$cond": [
                {"$eq": ["$entries.side", "DEBIT"]},
                "$entries.amount",
                {"$multiply": [-1, "$entries.amount"]},
            ]}},
        }},
    ]
    result = list(jnl_coll.aggregate(pipeline))
    return int(result[0]["signedSum"]) if result else 0


def reconcile_account(
    account_code: str,
    connection: MongoDBConnection,
    db_name: str,
    *,
    period_code: Optional[str] = None,
) -> ReconciliationResult:
    """Reconcile one account: subledger sum vs journal sum.

    Args:
        account_code: GL account code (e.g. "2000", "2100").
        period_code: "YYYY-MM" to scope to one period; None for all-time audit.
    """
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")
    jnl_coll = connection.get_collection(db_name, "journalEntries")

    sl_sum = _subledger_signed_sum(sl_coll, account_code, period_code)
    jnl_sum = _journal_signed_sum(jnl_coll, account_code, period_code)

    result = ReconciliationResult(
        account_code=account_code,
        period_code=period_code,
        subledger_sum=sl_sum,
        journal_sum=jnl_sum,
        checked_at=_now_utc(),
    )

    if not result.is_reconciled:
        logger.warning(
            "reconciliation FAIL account=%s period=%s sl=%d jnl=%d",
            account_code,
            period_code or "ALL-TIME",
            sl_sum,
            jnl_sum,
        )
    else:
        logger.debug(
            "reconciliation OK account=%s period=%s sum=%d",
            account_code,
            period_code or "ALL-TIME",
            sl_sum,
        )

    return result


def reconcile_all_accounts(
    connection: MongoDBConnection,
    db_name: str,
    *,
    period_code: Optional[str] = None,
) -> list[ReconciliationResult]:
    """Reconcile every posting account that has journaled subLedgerEntries in the given period."""
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")

    match: dict = {"status": "POSTED", "journalEntryId": {"$ne": ""}}
    if period_code:
        match["periodCode"] = period_code

    account_codes = sl_coll.distinct("controlAccountCode", match)
    return [
        reconcile_account(code, connection, db_name, period_code=period_code)
        for code in account_codes
    ]


def reconcile_all_accounts_batched(
    connection: MongoDBConnection,
    db_name: str,
    *,
    period_codes: list[str],
) -> list[ReconciliationResult]:
    """Reconcile every journaled account across the given periods in two aggregations.

    Equivalent to calling reconcile_all_accounts once per period and concatenating
    the results, but replaces the (periods × accounts × 2) aggregation fan-out with
    exactly two grouped aggregations — one per collection. Use this for the dashboard
    roll-up, where only the reconciled/break tally is needed.

    The checked set mirrors reconcile_all_accounts: one result per
    (controlAccountCode, period) that has journaled subLedgerEntries in that period.
    """
    if not period_codes:
        return []

    sl_coll = connection.get_collection(db_name, "subLedgerEntries")
    jnl_coll = connection.get_collection(db_name, "journalEntries")

    # Subledger signed sums grouped by (period, controlAccountCode), journaled rows only.
    sl_pipeline = [
        {"$match": {
            "status": "POSTED",
            "journalEntryId": {"$ne": ""},
            "periodCode": {"$in": period_codes},
        }},
        {"$group": {
            "_id": {"period": "$periodCode", "account": "$controlAccountCode"},
            "signedSum": {"$sum": {"$cond": [
                {"$eq": ["$side", "DEBIT"]},
                "$amount",
                {"$multiply": [-1, "$amount"]},
            ]}},
        }},
    ]
    sl_sums: dict[tuple[str, str], int] = {
        (r["_id"]["period"], r["_id"]["account"]): int(r["signedSum"])
        for r in sl_coll.aggregate(sl_pipeline)
    }

    # Journal signed sums grouped by (period, entries.accountCode).
    jnl_pipeline = [
        {"$match": {"periodCode": {"$in": period_codes}}},
        {"$unwind": "$entries"},
        {"$group": {
            "_id": {"period": "$periodCode", "account": "$entries.accountCode"},
            "signedSum": {"$sum": {"$cond": [
                {"$eq": ["$entries.side", "DEBIT"]},
                "$entries.amount",
                {"$multiply": [-1, "$entries.amount"]},
            ]}},
        }},
    ]
    jnl_sums: dict[tuple[str, str], int] = {
        (r["_id"]["period"], r["_id"]["account"]): int(r["signedSum"])
        for r in jnl_coll.aggregate(jnl_pipeline)
    }

    checked_at = _now_utc()
    results: list[ReconciliationResult] = []
    for (period, account), sl_sum in sl_sums.items():
        jnl_sum = jnl_sums.get((period, account), 0)
        result = ReconciliationResult(
            account_code=account,
            period_code=period,
            subledger_sum=sl_sum,
            journal_sum=jnl_sum,
            checked_at=checked_at,
        )
        if not result.is_reconciled:
            logger.warning(
                "reconciliation FAIL account=%s period=%s sl=%d jnl=%d",
                account, period, sl_sum, jnl_sum,
            )
        results.append(result)
    return results
