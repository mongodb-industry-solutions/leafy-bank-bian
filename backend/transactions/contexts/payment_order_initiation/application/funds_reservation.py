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

## No separate collection

The payment already carries everything a reservation record would repeat (account, amount),
so the only state is two scalars on the payment: `refs.fundsReservationId` (set at reserve)
and `refs.fundsReleasedAt` (set at release). Each transition is one transaction that flips
the payment marker and moves the balance together, so a replay cannot hold or release twice.
The released amount is the payment's `instructedAmount`; it must not change between stage 3
and release.

## Who releases

- **Stage 5**, inside the money-move transaction, immediately before the debit. This is the
  normal ending.
- **A rejection** (`release_for_payment`): any pre-execution terminal state frees the money.
  Callers are the saga's `_mark_rejected` and the operator decisions in `PaymentsService`.

Release is a no-op when nothing is held, so a late or repeated call is safe.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ID_FIELD = "refs.fundsReservationId"
RELEASED_AT_FIELD = "refs.fundsReleasedAt"


class _InsufficientFunds(Exception):
    """Raised inside the reserve transaction so the payment marker rolls back too."""


def reservation_id_for(payment_id: str) -> str:
    """Deterministic, so a replayed stage 3 names the same reservation."""
    return f"RSV-{payment_id.removeprefix('PAY-')}"


def reserve(ctx, *, now: Optional[datetime] = None) -> Optional[dict]:
    """Hold `ctx.instructed_amount` on the debtor account.

    Returns `{"reservationId": ...}`, or None when the available balance does not cover the
    amount (nothing is written). The balance check and the decrement are one conditional
    update, so two payments racing for the same balance cannot both succeed.
    """
    c = ctx.collections
    reservation_id = reservation_id_for(ctx.payment_id)

    existing = c.payments.find_one(
        {"paymentId": ctx.payment_id, ID_FIELD: {"$ne": None}, RELEASED_AT_FIELD: None}
    )
    if existing is not None:
        return {"reservationId": existing["refs"]["fundsReservationId"]}

    now = now or datetime.now(timezone.utc)
    amount = ctx.instructed_amount

    def callback(session) -> dict:
        # Marker first: a concurrent replay matches nothing here and cannot decrement twice.
        marked = c.payments.update_one(
            {"paymentId": ctx.payment_id, ID_FIELD: None},
            {"$set": {ID_FIELD: reservation_id, RELEASED_AT_FIELD: None}},
            session=session,
        )
        if marked.matched_count == 0:
            return {"reservationId": reservation_id}
        held = c.accounts.find_one_and_update(
            {"accountId": ctx.debtor_account_ref, "balance.available": {"$gte": amount}},
            {"$inc": {"balance.available": -amount, "balance.hold": amount},
             "$set": {"balance.updatedAt": now, "updatedAt": now}},
            session=session,
        )
        if held is None:
            raise _InsufficientFunds
        return {"reservationId": reservation_id}

    try:
        return _in_transaction(c.db, callback)
    except _InsufficientFunds:
        return None


def release(
    db, payment_id: str, *, reason: str, session=None, at: Optional[datetime] = None,
) -> Optional[dict]:
    """Return the held amount to the available balance. None when nothing was held.

    Pass `session` from a caller already inside a transaction (stage 5's money move); without
    one, the release opens its own so the marker flip and the balance restore stay atomic.
    `reason` is for the caller's log line; the payment's own event trail carries the why.
    """
    now = at or datetime.now(timezone.utc)

    def callback(txn_session) -> Optional[dict]:
        payment = db["payments"].find_one_and_update(
            {"paymentId": payment_id, ID_FIELD: {"$ne": None}, RELEASED_AT_FIELD: None},
            {"$set": {RELEASED_AT_FIELD: now}},
            session=txn_session,
            return_document=True,
        )
        if payment is None:
            return None
        amount = payment["instructedAmount"]
        db["accounts"].update_one(
            {"accountId": payment["debtor"]["accountId"]},
            {"$inc": {"balance.available": amount, "balance.hold": -amount},
             "$set": {"balance.updatedAt": now, "updatedAt": now}},
            session=txn_session,
        )
        logger.info("released funds reservation %s: %s",
                    payment["refs"]["fundsReservationId"], reason)
        return {"reservationId": payment["refs"]["fundsReservationId"], "amount": amount}

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
