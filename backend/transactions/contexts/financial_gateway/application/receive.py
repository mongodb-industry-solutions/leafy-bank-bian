"""Inbound stage 1 — receive and canonicalise an external pacs.008 (FR-1.IN1..3).

BIAN FinancialGateway (SD 30542) + PaymentOrderInitiation. The inbound counterpart of
`payment_order_initiation/application/capture.py`, and deliberately shaped like it: same
`run(ctx) -> None` signature, same "persist, then advance" discipline, so the saga sequences
both the same way.

## The order is the requirement, not an implementation detail

Her L363 gives the sequence exactly:

    external pacs.008
      -> paymentMessages (direction: INBOUND, paymentId: null)   <- FIRST
      -> parse and validate
      -> generate paymentId
      -> payments (direction: INBOUND, rail: WIRE, status: RECEIVED)
      -> back-link paymentMessages.paymentId

and L368-372 gives five reasons the message is persisted first: the original is not lost if
parsing fails, duplicates are detectable before a duplicate payment exists, the message can
be replayed, the payment can be reconstructed from it, and it stands as an immutable audit
record. **Outbound is the mirror** (`payments` first, message second) because there the
instruction is the trigger; here the message is. Do not "tidy" these into one order.

The practical consequence: a message this stage REJECTS is still on disk. That is why
`inbound_pacs008.parse` is called after the insert, not before it.

Reads  ctx: inbound_message, collections, now
Writes ctx: payment_oid, payment_id, end_to_end_id, txn_code, direction, debtor/creditor
            snapshots, inbound_message_id, payment_doc, current_state (or halts on replay)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from contexts.financial_gateway.domain import inbound_pacs008
from contexts.financial_gateway.domain.inbound_documents import inbound_message_doc
from contexts.payment_order_initiation.domain import checks, lifecycle, payment_document
from contexts.payment_rail.domain import execution_documents
from process.payment_context import PaymentContext
from shared.refs import derive_ref

logger = logging.getLogger(__name__)

STAGE = "1 receive"

INBOUND = "INBOUND"


def run(ctx: PaymentContext) -> None:
    c = ctx.collections
    ctx.direction = INBOUND
    ctx.now = datetime.now(timezone.utc)

    if not ctx.inbound_message:
        raise ValueError("No inbound message supplied to the financial gateway.")

    # --- 1. persist the raw message FIRST (FR-1.IN1) -------------------------
    message_oid = ObjectId()
    ctx.inbound_message_id = derive_ref("PM", message_oid)
    message_doc = inbound_message_doc(
        oid=message_oid,
        message=ctx.inbound_message,
        now=ctx.now,
    )
    messages = _messages(ctx)
    if messages is not None:
        messages.insert_one(message_doc)

    # --- 2. parse and validate (after the insert, deliberately) --------------
    # A MessageRejected here leaves the stored message untouched and unlinked
    # (`paymentId: null`), which is exactly her "not lost if parsing fails".
    parsed = inbound_pacs008.parse(ctx.inbound_message)

    # --- 3. duplicate detection, before a duplicate payment exists -----------
    ctx.idempotency_key = inbound_pacs008.idempotency_key(parsed)
    if ctx.idempotency_key:
        existing = c.payments.find_one({"idempotency.idempotencyKey": ctx.idempotency_key})
        if existing is not None:
            logger.info(
                "Inbound replay for %s — returning existing paymentId=%s",
                ctx.idempotency_key, existing["paymentId"],
            )
            _link_message(messages, message_oid, existing["paymentId"])
            _stamp_replay_evidence(c, existing, ctx.idempotency_key)
            ctx.stop(existing)
            return

    # --- 4. resolve what the message names -----------------------------------
    # The DEBTOR is external (that is what inbound means), so its snapshot comes straight
    # off the message — the same shape `external_party_snapshot` builds for an outbound
    # external creditor, with the sides swapped.
    ctx.creditor_party = None
    ctx.is_external_creditor = False
    ctx.is_internal = False

    # The claimed beneficiary is NOT resolved here. Her L387: at this stage it is "an
    # account number and name asserted by the sending bank". Stage 2 (`resolve.py`) does the
    # lookup and the name match, and it is the stage allowed to fail the payment for it.
    # Stage 1 only records what was claimed.
    ctx.claimed_creditor = parsed["claimedCreditor"]
    ctx.inbound_parsed = parsed

    ctx.instructed_amount = parsed["amount"]
    ctx.instructed_currency = parsed["currency"]
    ctx.payment_rail = parsed["rail"]
    ctx.payment_type = parsed["type"]
    ctx.charge_bearer = parsed.get("chargeBearer") or "SLEV"
    ctx.remittance_unstructured = parsed.get("remittanceUnstructured")
    ctx.remittance_reference = parsed.get("remittanceReference")
    ctx.requested_execution_date = ctx.now.date()

    # --- 5. mint OUR identifiers ---------------------------------------------
    # The sender's references are preserved on the stored message and in the debtor
    # snapshot; they never become our primary key. `uetr` is the one exception and is
    # carried through — see `inbound_pacs008.parse`.
    ctx.payment_oid = ObjectId()
    ctx.payment_id = derive_ref("PAY", ctx.payment_oid)
    ctx.end_to_end_id = derive_ref("E2E", ctx.payment_oid, last_n=12)
    ctx.txn_code = "PMNT-RCDT-ESCT"  # ISO: received credit transfer

    # --- 6. build and persist the canonical payment --------------------------
    ctx.payment_doc = payment_document.build_inbound(ctx)
    try:
        c.payments.insert_one(ctx.payment_doc)
    except DuplicateKeyError:
        existing = (
            c.payments.find_one({"idempotency.idempotencyKey": ctx.idempotency_key})
            if ctx.idempotency_key else None
        )
        if existing is None:
            raise
        logger.info(
            "Concurrent inbound replay for %s — returning existing paymentId=%s",
            ctx.idempotency_key, existing["paymentId"],
        )
        _link_message(messages, message_oid, existing["paymentId"])
        _stamp_replay_evidence(c, existing, ctx.idempotency_key)
        ctx.stop(existing)
        return

    # --- 7. back-link the message to the payment (FR-1.IN2) ------------------
    _link_message(messages, message_oid, ctx.payment_id)

    ctx.current_state = lifecycle.DRAFT
    lifecycle.advance_ctx(
        ctx, lifecycle.RECEIVED,
        actor="financial-gateway",
        reason=(
            f"Inbound pacs.008 received from "
            f"{parsed['debtor'].get('bankName') or parsed['debtor'].get('bic') or 'sending bank'}"
        ),
    )


def _messages(ctx: PaymentContext):
    """The `paymentMessages` handle, falling back to `db[name]`.

    Same tolerance as `execute._collection`: a context built before this stage existed
    reaches the collection through `db` rather than raising an AttributeError mid-stage.
    """
    collections = ctx.collections
    if collections is None:
        return None
    handle = getattr(collections, "payment_messages", None)
    if handle is not None:
        return handle
    db = getattr(collections, "db", None)
    return None if db is None else db[execution_documents.PAYMENT_MESSAGES]


def _link_message(messages, message_oid: ObjectId, payment_id: str) -> None:
    """Step 4 of her sequence: stamp the payment back onto the stored message.

    The ONE permitted update on an inbound message document. `paymentMessages` is otherwise
    insert-only (execution_documents' "append-only" contract), and this is the forward
    reference that contract explicitly anticipates: the message is written before the
    payment exists, so its `paymentId` cannot be known at insert.
    """
    if messages is None:
        return
    messages.update_one({"_id": message_oid}, {"$set": {"paymentId": payment_id}})


def _stamp_replay_evidence(c, winner: dict, key: str) -> None:
    """Visible evidence on the winning payment that a duplicate message was absorbed.

    Same device as the outbound `capture._stamp_replay_evidence`: without it a resent
    message returns the winner with no trace, and the demo beat ("the same wire arriving
    twice credits the customer once") would be invisible in the timeline.
    """
    entry = checks.check(
        STAGE, "inbound_duplicate_absorbed", checks.PASS,
        mode=checks.SYNC,
        detail=(
            f"Inbound message key {key} absorbed a duplicate — returning existing payment "
            f"{winner.get('paymentId')}. No second credit."
        ),
        actor="financial-gateway",
        at=datetime.now(timezone.utc),
    )
    checks.append_checks(c.payments, winner["_id"], [entry])
