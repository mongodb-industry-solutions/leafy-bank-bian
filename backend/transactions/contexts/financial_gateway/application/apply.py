"""Inbound stages 5, 6 and 7 — acknowledge, credit, and settle on arrival.

Three stages in one module because for inbound they are one atomic beat: the money is
already here, so telling the sender we accept it, crediting the customer, and recognising
the settlement are not separated by any external wait. Outbound needs three modules because
a rail sits between each pair; inbound has no such gap.

| stage | outbound                        | inbound (here)                            |
|-------|---------------------------------|-------------------------------------------|
| 5     | build pacs.008, submit to rail  | build pacs.002, transmit to sender        |
| 6     | Dr customer / Cr clearing       | **Dr clearing / Cr customer** (FR-6.IN1)  |
| 7     | await the rail's settlement     | **settled on arrival** (D-IN4)            |

## Stage 6 — the mirror, and why it needs almost no code

Her L1036: *"the entire pipeline is reused unchanged ... what's different: only the direction
of the debit/credit legs."* That falls out for free, because `posting_rules` reads each
account's own `gl.accountCode` rather than a side-to-code table — so passing payer=clearing
and payee=customer produces `Dr 1131 / Cr customer deposit` with no rule change at all. The
ledger's CDC pipeline never learns that inbound exists.

## Stage 7 — settle-on-arrival (D-IN4, Kiran 2026-09-28)

Outbound defers settlement because a real wire genuinely waits on a rail confirmation. For
inbound the funds arrived **before** the message was parsed, so there is nothing to wait for:
the settlement position is written and confirmed in the same pass, and the mirror event
`Dr Nostro / Cr Wire Clearing` (FR-7.IN1) posts immediately so 1131 nets to zero.

⚠️ **Demo note:** this makes the inbound settlement panel resolve instantly rather than
showing a visible clearing window. That is correct behaviour, not a bug — flagged because the
outbound story leans on that window being visible.

Reads  ctx: payment_doc, creditor_account, instructed_amount, inbound_message_id
Writes ctx: result, payment_doc, current_state
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from contexts.financial_gateway.domain.inbound_documents import status_response_doc
from contexts.payment_order_initiation.domain import checks, lifecycle
from contexts.payment_rail import documents
from contexts.payment_settlement import settle
from process.payment_context import PaymentContext

logger = logging.getLogger(__name__)

STAGE_ACK = "5 acknowledge"
STAGE_POST = "6 credit"
STAGE_SETTLE = "7 settle"

# The rail's clearing account — the source of an inbound credit. Its mirror image outbound:
# there it RECEIVES the customer's debit, here it FUNDS the customer's credit, and in both
# cases 1131 nets to zero once settlement confirms.
_CLEARING_ACCOUNT_BY_RAIL = {"WIRE": "ACC-CLEARING-WIRE"}


def run(ctx: PaymentContext) -> None:
    if ctx.direction != "INBOUND":  # pragma: no cover - the saga never routes it here
        return

    now = datetime.now(timezone.utc)
    ctx.now = now

    _acknowledge(ctx, now)
    _credit(ctx, now)
    _settle_on_arrival(ctx, now)


# --- stage 5: the status response --------------------------------------------

def _acknowledge(ctx: PaymentContext, now: datetime) -> None:
    """FR-5.IN1/2/3 — generate the pacs.002, store it, advance to ACCEPTED.

    ⚠️ **No `paymentExecutions` document.** Her L944 is explicit: that collection exists
    specifically for tracking OUTBOUND rail-message generation attempts and stays
    outbound-only by design. An inbound payment has no execution attempt — nothing was
    submitted anywhere. The status response lands in `paymentMessages` instead, tagged
    `direction: OUTBOUND, purpose: STATUS_RESPONSE`.
    """
    oid = ObjectId()
    message = status_response_doc(
        oid=oid,
        payment=ctx.payment_doc,
        accepted=True,
        reason_code=None,
        original_message_ref=ctx.inbound_message_id,
        now=now,
    )
    _messages(ctx).insert_one(message)

    checks.append_checks(ctx.collections.payments, ctx.payment_oid, [
        checks.check(
            STAGE_ACK, "status_response_transmitted", checks.PASS, mode=checks.SYNC,
            detail=(
                f"pacs.002 {message['paymentMessageId']} (ACCP) transmitted to the sending "
                f"bank, confirming the payment will be applied. SIMULATED."
            ),
            actor="financial-gateway", at=now,
        )
    ])

    lifecycle.advance_ctx(
        ctx, lifecycle.ACCEPTED,
        actor="financial-gateway",
        reason="Positive status report (pacs.002 ACCP) transmitted to the sending bank",
        extra={
            "clearing.submittedAt": now,
            "clearing.statusCode": "ACCP",
            "refs.statusResponseMessageId": message["paymentMessageId"],
        },
    )


# --- stage 6: the credit ------------------------------------------------------

def _credit(ctx: PaymentContext, now: datetime) -> None:
    """FR-6.IN1 — `Dr Wire Clearing / Cr Customer Deposit`, the reverse of outbound FR-6.3.

    The money move is one ACID transaction: clearing debited, customer credited, the
    `transactions` doc written. The ledger service picks that doc up by change stream and
    derives the GL legs exactly as it does for an outbound payment — it needs no inbound
    branch, because `posting_rules` reads each account's own `gl.accountCode`.
    """
    c = ctx.collections
    clearing_id = _CLEARING_ACCOUNT_BY_RAIL.get(ctx.payment_rail)
    if not clearing_id:
        raise ValueError(
            f"no clearing account mapped for inbound rail {ctx.payment_rail!r}"
        )
    clearing = c.accounts.find_one({"accountId": clearing_id})
    if not clearing:
        raise ValueError(
            f"clearing account {clearing_id!r} not found — seed it before applying "
            f"inbound {ctx.payment_rail} payments"
        )

    amount = ctx.instructed_amount
    creditor_account_ref = ctx.creditor_account_ref

    def callback(session):
        # DEBIT the clearing account. No `balance.available` floor: a clearing account is a
        # bank-internal position, not a customer balance, and the funds demonstrably arrived
        # — refusing our own credit for "insufficient funds" on a control account would be
        # wrong. (The outbound path DOES floor the customer's debit, correctly.)
        c.accounts.find_one_and_update(
            {"accountId": clearing_id},
            {
                "$inc": {
                    "balance.current": -amount,
                    "balance.available": -amount,
                    "balance.ledger": -amount,
                },
                "$set": {"balance.updatedAt": now, "updatedAt": now},
            },
            session=session,
            return_document=True,
        )
        creditor_after = c.accounts.find_one_and_update(
            {"accountId": creditor_account_ref},
            {
                "$inc": {
                    "balance.current": amount,
                    "balance.available": amount,
                    "balance.ledger": amount,
                },
                "$set": {"balance.updatedAt": now, "updatedAt": now},
            },
            session=session,
            return_document=True,
        )
        if creditor_after is None:
            # The clearing debit committed and the customer credit did not — money
            # destroyed. Stage 2 resolved this account, so it is unreachable; the assertion
            # is what keeps it that way. Never remove it to "handle" a missing creditor.
            raise ValueError(
                f"Beneficiary account {creditor_account_ref} did not match at posting "
                "time — the credit was rolled back."
            )

        txn_doc = documents.transaction_doc(
            payment_oid=ctx.payment_oid,
            payment_id=ctx.payment_id,
            # payer = the clearing account, payee = the customer. THE mirror, and the only
            # thing that differs from an outbound posting.
            debtor_account=clearing,
            debtor_customer={},
            creditor_account=ctx.creditor_account,
            creditor_customer=ctx.creditor_customer,
            debtor_after=clearing,
            amount=amount,
            currency=ctx.instructed_currency,
            payment_rail=ctx.payment_rail,
            txn_code=ctx.txn_code,
            is_internal=False,
            payment_execution_id=None,
            now=now,
            direction="INBOUND",
        )
        c.transactions.insert_one(txn_doc, session=session)

        # The BENEFICIARY is notified on an inbound payment — they received money. The
        # originator is another bank's customer and is not ours to notify; `direction`
        # selects that branch in the builder rather than this stage faking a sender.
        notif_docs = documents.build_notifications(
            payment_oid=ctx.payment_oid,
            payment_id=ctx.payment_id,
            txn_id=txn_doc["txnId"],
            debtor_account=clearing,
            creditor_account=creditor_after,
            debtor_customer={},
            debtor_after=clearing,
            amount=amount,
            currency=ctx.instructed_currency,
            payment_rail=ctx.payment_rail,
            is_internal=False,
            now=now,
            direction="INBOUND",
            originator_name=((ctx.inbound_parsed or {}).get("debtor") or {}).get("name"),
        )
        if notif_docs:
            c.notifications.insert_many(notif_docs, session=session)
        return txn_doc

    txn_doc = _in_transaction(ctx, callback)

    lifecycle.advance_ctx(
        ctx, lifecycle.IN_PROGRESS,
        actor="financial-gateway",
        reason=(
            f"Funds applied: {ctx.instructed_currency} {amount:,.2f} credited to "
            f"{creditor_account_ref}"
        ),
        extra={"refs.transactionId": txn_doc.get("txnId")},
    )
    ctx.result = ctx.collections.payments.find_one({"_id": ctx.payment_oid})


def _in_transaction(ctx, callback):
    """Run `callback` in an ACID transaction, tolerating a fake/session-less test db.

    Same accommodation `execute._money_move` makes: the hermetic FakeDb has no session
    support, so the callback runs directly there. The real driver always gets a session.
    """
    client = getattr(getattr(ctx.collections, "db", None), "client", None)
    if client is None:
        return callback(None)
    with client.start_session() as session:
        return session.with_transaction(callback)


# --- stage 7: settlement ------------------------------------------------------

def _settle_on_arrival(ctx: PaymentContext, now: datetime) -> None:
    """FR-7.IN1/2 — the mirror settlement event, confirmed immediately (D-IN4).

    `expectedPosition` comes from the inbound message's instructed amount (FR-7.IN2), not
    from a routing commitment — inbound has no orchestration stage to make one.

    The second ledger event (`Dr Nostro / Cr Wire Clearing`) is emitted by the ledger's
    settlement worker off `settlementStatus == SETTLED`, exactly as outbound's is. Writing
    the position with that status IS the trigger; this stage posts no legs itself, which is
    what keeps the domain boundary (the payment path never writes GL data) intact.
    """
    position_id = _write_position(ctx, now)

    checks.append_checks(ctx.collections.payments, ctx.payment_oid, [
        checks.check(
            STAGE_SETTLE, "settlement_confirmed", checks.PASS, mode=checks.SYNC,
            detail=(
                f"Inbound settlement confirmed on arrival — the funds were already with us "
                f"when the message was received, so there is no clearing window to await. "
                f"Position {position_id}: Dr Nostro / Cr Wire Clearing "
                f"({settle._WIRE_CLEARING_CODE}) nets the clearing position to zero."
            ),
            actor="payment-settlement-service", at=now,
        )
    ])

    lifecycle.advance_ctx(
        ctx, lifecycle.SETTLED,
        actor="payment-settlement-service",
        reason="Inbound payment settled on arrival",
        extra={
            "lifecycle.settlementStatus": "SETTLED",
            "clearing.settledAt": now,
            "clearing.settlementDate": (
                (ctx.inbound_parsed or {}).get("settlementDate")
                or now.date().isoformat()
            ),
            # ⚠️ The routing field the ledger's `settlement_worker` needs to build the
            # mirror event (FR-7.IN1). Outbound `settle.py` stamps it at the same
            # transition; without it here the worker raises on the inbound payment and
            # crash-loops, blocking every settlement queued behind it (found live,
            # 2026-09-29). The value comes from settle's own model table — the inbound
            # story is "via correspondent" — so the two spellings cannot drift.
            "clearing.settlementAccountCode": _INBOUND_SETTLEMENT_CODE(),
            "refs.settlementPositionId": position_id,
        },
    )
    ctx.result = ctx.collections.payments.find_one({"_id": ctx.payment_oid})


def _INBOUND_SETTLEMENT_CODE() -> str:
    """The GL code of the settlement account an inbound wire settles through.

    Read from `settle.py`'s own model table — the CORRESPONDENT model, which is the inbound
    story (the funds came from a correspondent bank into our nostro) — rather than restated
    here, so the two spellings cannot drift. Reaching into the private `_SETTLEMENT_MODELS`
    dict is deliberate: a public re-export would be a second name for one fact.
    """
    return settle._SETTLEMENT_MODELS["CORRESPONDENT"]["settlementAccountCode"]


def _write_position(ctx: PaymentContext, now: datetime) -> str:
    """One `settlementPositions` document, reusing the outbound shape unchanged.

    Her L1150: *"no new collections or fields — settlementPositions is already generalized
    enough to support either direction via the existing paymentId link plus the payment's
    direction field."*
    """
    from shared.refs import derive_ref

    oid = ObjectId()
    position_id = derive_ref("SP", oid)
    amount = ctx.instructed_amount
    doc = {
        "_id": oid,
        "settlementPositionId": position_id,
        "paymentId": ctx.payment_id,
        "rail": ctx.payment_rail,
        "model": "CORRESPONDENT",
        "modelLabel": "Inbound via correspondent",
        "clearingAccountCode": settle._WIRE_CLEARING_CODE,
        # From the same model row as the label — the first version hardcoded "1121" beside
        # a "CORRESPONDENT" label (whose code is 1111), an incoherent pair on the position.
        "settlementAccountCode": _INBOUND_SETTLEMENT_CODE(),
        # FR-7.IN2 — derived from the inbound message's instructed amount, not a routing
        # commitment (which does not exist for inbound).
        "expectedAmount": amount,
        "expectedCurrency": ctx.instructed_currency,
        "actualAmount": amount,
        "actualCurrency": ctx.instructed_currency,
        "grossAmount": amount,
        "currency": ctx.instructed_currency,
        "outcome": settle.MATCHED,
        "settlementStatus": "SETTLED",
        "batchRef": f"INBOUND-{now.date().isoformat()}",
        "statusCode": "ACCP",
        "rejectionCode": None,
        "returnCode": None,
        "simulated": True,
        "createdAt": now,
        "sourceSystem": "leafy-bank-payments-service",
    }
    ctx.collections.db["settlementPositions"].insert_one(doc)
    return position_id


def _messages(ctx: PaymentContext):
    collections = ctx.collections
    handle = getattr(collections, "payment_messages", None)
    if handle is not None:
        return handle
    return collections.db["paymentMessages"]
