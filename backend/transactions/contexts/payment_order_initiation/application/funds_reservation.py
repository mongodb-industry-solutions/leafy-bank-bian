"""Funds reservation — stage 3's hold on the debtor's available balance.

Doina (Oct 2026): *check the available balance, create a hold/reservation against the
account, reduce the available balance, return the reservation id. During posting, release the
reservation, and post the debit.*

## The three balance fields

An account carries `balance.{current, available, ledger, hold}`. A reservation moves money
between two of them and touches neither of the others:

    reserve   available -= amount     hold += amount      (current, ledger unchanged)
    release   available += amount     hold -= amount
    debit     current, available, ledger -= amount        (stage 5, after the release)

So between stage 3 and stage 5 the customer cannot spend the reserved money on a second
payment, yet the ledger still shows it as theirs. Without this, two concurrent wires could
both pass the stage 3 pre-flight check against the same balance and one would fail late, in
stage 5's ACID transaction, after fraud scoring and routing had already been spent on it.

## One reservation per payment

`fundsReservations` has a unique index on `paymentId`, and `reserve` is idempotent: a saga
re-entry at stage 3 (a resume from PENDING_APPROVAL, a retried request) finds its ACTIVE
reservation and returns it instead of holding the money twice.

## Who releases

- **Stage 5**, inside the money-move transaction, immediately before the debit. This is the
  normal ending.
- **A rejection** (`release_for_payment`): any pre-execution terminal state frees the money.
  Callers are the saga's `_mark_rejected` and the operator decisions in `PaymentsService`.

Release is a no-op when nothing is ACTIVE, so a late or repeated call is safe.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

COLLECTION = "fundsReservations"

ACTIVE = "ACTIVE"
RELEASED = "RELEASED"


def reservation_id_for(payment_id: str) -> str:
    """Deterministic, so a replayed stage 3 names the same reservation."""
    return f"RSV-{payment_id.removeprefix('PAY-')}"


def reserve(ctx, *, now: Optional[datetime] = None) -> Optional[dict]:
    """Hold `ctx.instructed_amount` on the debtor account.

    Returns the ACTIVE reservation, or None when the available balance does not cover the
    amount (nothing is written). The balance check and the decrement are one conditional
    update, so two payments racing for the same balance cannot both succeed.
    """
    c = ctx.collections
    reservations = c.db[COLLECTION]

    existing = reservations.find_one({"paymentId": ctx.payment_id, "status": ACTIVE})
    if existing is not None:
        return existing

    now = now or datetime.now(timezone.utc)
    amount = ctx.instructed_amount
    doc = {
        "reservationId": reservation_id_for(ctx.payment_id),
        "paymentId": ctx.payment_id,
        "accountId": ctx.debtor_account_ref,
        "amount": amount,
        "currency": (ctx.debtor_account or {}).get("currency"),
        "status": ACTIVE,
        "reason": "Funds reserved at stage 3 validation",
        "createdAt": now,
        "releasedAt": None,
        "releaseReason": None,
    }

    def callback(session) -> Optional[dict]:
        held = c.accounts.find_one_and_update(
            {"accountId": ctx.debtor_account_ref, "balance.available": {"$gte": amount}},
            {"$inc": {"balance.available": -amount, "balance.hold": amount},
             "$set": {"balance.updatedAt": now, "updatedAt": now}},
            session=session,
        )
        if held is None:
            return None
        reservations.insert_one(dict(doc), session=session)
        return doc

    return _in_transaction(c.db, callback)


def release(
    db, payment_id: str, *, reason: str, session=None, at: Optional[datetime] = None,
) -> Optional[dict]:
    """Return the held amount to the available balance. None when nothing was ACTIVE.

    Pass `session` from a caller already inside a transaction (stage 5's money move); without
    one, the release opens its own so the status flip and the balance restore stay atomic.
    """
    now = at or datetime.now(timezone.utc)

    def callback(txn_session) -> Optional[dict]:
        released = db[COLLECTION].find_one_and_update(
            {"paymentId": payment_id, "status": ACTIVE},
            {"$set": {"status": RELEASED, "releasedAt": now, "releaseReason": reason}},
            session=txn_session,
            return_document=True,
        )
        if released is None:
            return None
        db["accounts"].update_one(
            {"accountId": released["accountId"]},
            {"$inc": {"balance.available": released["amount"],
                      "balance.hold": -released["amount"]},
             "$set": {"balance.updatedAt": now, "updatedAt": now}},
            session=txn_session,
        )
        return released

    if session is not None:
        return callback(session)
    return _in_transaction(db, callback)


def release_for_payment(db, payment_id: str, *, reason: str) -> Optional[dict]:
    """Free a reservation because the payment ended before it was debited.

    Never raises: it runs on rejection paths whose real error must not be replaced by a
    bookkeeping failure. A reservation that could not be released stays ACTIVE and visible.
    """
    try:
        return release(db, payment_id, reason=reason)
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception(
            "could not release the funds reservation for payment %s", payment_id
        )
        return None


def _in_transaction(db, callback: Callable):
    with db.client.start_session() as session:
        return session.with_transaction(callback)
