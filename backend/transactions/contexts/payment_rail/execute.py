"""Stage 5 — execution (BIAN PaymentRail, SD 47741).

Doina stage 5: wire / ISO 20022, ACH, cards. Today: the internal book transfer.

## The ACID block — do not restructure

`_money_move` below is moved **unmodified** from `payments_service.py:201-277`. It performs
the money move as ONE multi-document ACID transaction: debtor balance, creditor balance,
one `transactions` doc, one notification, and the payment status flip.

It spans four aggregates — debtor account, creditor account, payment, notification — which
textbook DDD says to split into a saga. **We keep it.** Multi-document ACID across
aggregates *is the MongoDB capability being demoed* (doc 09 §5). Documented here so nobody
"corrects" it, and so the exception stays one block long instead of spreading.

## Why the SETTLED transition is in here

For a synchronous internal book transfer, settlement **is** the money move — so the
`SETTLED` transition belongs inside this transaction, atomic with the two balance updates.
That is a design choice, not a leftover: a payment can never be observed as settled without
the balances having moved, or vice versa.

When stage 7 becomes real (nostro/vostro, value dates, rail confirmation), settlement stops
being simultaneous and this transition moves to `payment_settlement/settle.py`. The state
machine is what makes that a one-line change.

Reads  ctx: everything resolved and produced by stages 1-4
Writes ctx: current_state -> SUBMITTED -> IN_PROGRESS -> SETTLED; result

TODO (Doina stage 5 Key Features):
  - rail adapters behind one outbound port: `adapters/{wire_pacs008,ach_nacha,card_iso8583}.py`.
    Three implementations of one port — that is the structural form of the canonical-envelope
    premise. If ACH ends up needing its own context, the envelope has failed (doc 09 §3).
  - `canonicalJsonStorage` — the rail-specific execution artifact (pain.001 / pacs.008 XML).
  - `paymentExecutions`, append-only, one doc per execution *attempt* (plan §8 item 4).
    Append-only is the whole point: repairs, recalls, returns and retries create additional
    artifacts rather than overwrite the original — which is how stage 9 largely falls out
    for free.
  - execution must be gated on the stage 4b authorization decision.
"""

from __future__ import annotations

from pymongo.client_session import ClientSession

from contexts.payment_order_initiation.domain import lifecycle
from contexts.payment_rail import documents
from process.payment_context import PaymentContext


def run(ctx: PaymentContext) -> None:
    lifecycle.advance_ctx(
        ctx, lifecycle.SUBMITTED,
        actor="transactions-service",
        reason=f"Submitted to {ctx.payment_rail} rail",
    )
    lifecycle.advance_ctx(
        ctx, lifecycle.IN_PROGRESS,
        actor="transactions-service",
        reason="Rail execution started",
    )
    ctx.result = _money_move(ctx)


def _money_move(ctx: PaymentContext) -> dict:
    c = ctx.collections
    now = ctx.now
    amount = ctx.instructed_amount

    def callback(session: ClientSession) -> dict:
        debtor_after = c.accounts.find_one_and_update(
            {
                "accountId": ctx.debtor_account_ref,
                "balance.available": {"$gte": amount},
            },
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
        if debtor_after is None:
            raise ValueError("Insufficient funds at settlement time.")
        c.accounts.find_one_and_update(
            {"accountId": ctx.creditor_account_ref},
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

        txn_doc = documents.transaction_doc(
            payment_oid=ctx.payment_oid,
            payment_id=ctx.payment_id,
            debtor_account=ctx.debtor_account,
            debtor_customer=ctx.debtor_customer,
            creditor_account=ctx.creditor_account,
            creditor_customer=ctx.creditor_customer,
            debtor_after=debtor_after,
            amount=amount,
            currency=ctx.instructed_currency,
            payment_rail=ctx.payment_rail,
            txn_code=ctx.txn_code,
            is_internal=ctx.is_internal,
            now=now,
        )
        c.transactions.insert_one(txn_doc, session=session)

        notif_docs = documents.build_notifications(
            payment_oid=ctx.payment_oid,
            payment_id=ctx.payment_id,
            txn_id=txn_doc["txnId"],
            debtor_account=ctx.debtor_account,
            creditor_account=ctx.creditor_account,
            debtor_customer=ctx.debtor_customer,
            debtor_after=debtor_after,
            amount=amount,
            currency=ctx.instructed_currency,
            payment_rail=ctx.payment_rail,
            is_internal=ctx.is_internal,
            now=now,
        )
        if notif_docs:
            c.notifications.insert_many(notif_docs, session=session)

        # Settlement, atomic with the money move. See the module docstring.
        return lifecycle.advance_ctx(
            ctx, lifecycle.SETTLED,
            actor="transactions-service",
            reason="Book transfer settled",
            session=session,
            extra={"clearing.settledAt": now},
        )

    with c.db.client.start_session() as session:
        return session.with_transaction(callback)
