"""The payment lifecycle state machine (decision D1).

**Nothing writes `payments.status` or `payments.lifecycle` except this module.** That is the
whole point: state, actor, reason, and timestamp only stay populated across nine stages if
there is exactly one way to change state.

## Shape

The canonical schema (`doinas-research/propose_payments.json`) is explicit:

    lifecycle.currentState is the SOURCE OF TRUTH for the payment's state; the top-level
    `status` field mirrors it for query and index compatibility.

So every transition writes both. `status` is a mirror, never an independent value.

`lifecycle.events[]` is **append-only** — chronological, one entry per transition, never
rewritten or reordered. A correction is a new entry, not an edit.

## Divergence from Doina's doc, deliberate

Her sequence omits `ROUTED`. D1 inserts it after `FINAL_VALIDATED`, because a destination
must be resolved before it can be fraud-scored. Still on the "tell Doina" list (doc 10).

## The second axis

`postingStatus` and `settlementStatus` advance independently of `currentState` (D1) — the
ledger service owns posting via change streams and never touches this state machine.

**`postingStatus` is modelled as of stage 6, and NOT here.** The ledger service writes it
directly (`ledger/services/posting_writeback_service.py`), because the canonical spec assigns
the field to that service in so many words: *"Owned by the ledger service via CDC, never
written by the payment path."* It also advances `currentState` to `POSTED`, guarded on
`IN_PROGRESS`, using the same `events[]` shape `_event` produces below — asserted by
`test_the_written_event_matches_the_state_machines_own_shape` rather than by importing this
module across a service boundary. So the "nothing writes `payments.lifecycle` except this
module" rule above now has exactly one documented exception, and it is spec-mandated.

`settlementStatus` is still unmodelled — stage 7 owns it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional


class StepUpRequired(ValueError):
    """A payment hit the step-up gate with an insufficient factor.

    NOT a rejection. The saga catches it separately from `ValueError` and HOLDS the payment
    (status stays `INITIATED`, `stepUpRequired: true`) instead of marking it REJECTED, so the
    channel can collect a second factor and resume the SAME payment — one document, one id
    (Kiran, 2026-09-09). Subclasses ValueError so it maps to a 400 at the route if it ever
    escapes the saga; the saga is expected to handle it first.
    """

logger = logging.getLogger(__name__)

# --- states ------------------------------------------------------------------

DRAFT = "DRAFT"
INITIATED = "INITIATED"
VALIDATED = "VALIDATED"
ENRICHED = "ENRICHED"
FINAL_VALIDATED = "FINAL_VALIDATED"
ROUTED = "ROUTED"
AUTHORISED = "AUTHORISED"
APPROVED = "APPROVED"
SUBMITTED = "SUBMITTED"
IN_PROGRESS = "IN_PROGRESS"
POSTED = "POSTED"
SETTLED = "SETTLED"
RECONCILED = "RECONCILED"

# Terminals. A payment in one of these never advances again.
REJECTED = "REJECTED"
FAILED = "FAILED"
RETURNED = "RETURNED"
CANCELLED = "CANCELLED"
REVERSED = "REVERSED"
REFUNDED = "REFUNDED"

TERMINALS = frozenset({REJECTED, FAILED, RETURNED, CANCELLED, REVERSED, REFUNDED})

HAPPY_PATH = [
    DRAFT, INITIATED, VALIDATED, ENRICHED, FINAL_VALIDATED, ROUTED,
    AUTHORISED, APPROVED, SUBMITTED, IN_PROGRESS, POSTED, SETTLED, RECONCILED,
]

# Which terminals are reachable depends on whether money has moved.
#   before SUBMITTED — the payment can still be refused outright
#   from SUBMITTED on — money is in flight, so a correction is a compensating outcome,
#                       never a refusal. A settled payment cannot become REJECTED.
_PRE_EXECUTION_TERMINALS = frozenset({REJECTED, CANCELLED, FAILED})
_POST_EXECUTION_TERMINALS = frozenset({FAILED, RETURNED, REVERSED, REFUNDED})

_IN_FLIGHT_FROM = HAPPY_PATH.index(SUBMITTED)

_FORWARD = {a: {b} for a, b in zip(HAPPY_PATH, HAPPY_PATH[1:])}
_FORWARD[RECONCILED] = set()

# POSTED is stamped by the ledger service, asynchronously, and may arrive after SETTLED —
# the posting axis is independent (D1). Allow IN_PROGRESS -> SETTLED to skip it.
_FORWARD[IN_PROGRESS] = {POSTED, SETTLED}


def _terminals_for(state: str) -> frozenset:
    idx = HAPPY_PATH.index(state)
    return _POST_EXECUTION_TERMINALS if idx >= _IN_FLIGHT_FROM else _PRE_EXECUTION_TERMINALS


TRANSITIONS = {
    state: frozenset(successors | _terminals_for(state))
    for state, successors in _FORWARD.items()
}
for _t in TERMINALS:
    TRANSITIONS[_t] = frozenset()


class IllegalTransition(RuntimeError):
    """Raised when a stage tries to move a payment somewhere it cannot go.

    A bug, not a business outcome — business rejections go through `reject()`.
    """


# --- transitions -------------------------------------------------------------

def initial_block(*, actor: str, actor_type: str, reason: str, now: datetime) -> dict:
    """The `lifecycle{}` block for a payment being created at DRAFT.

    Pure — used by `payment_document.build`, which does no I/O.
    """
    return {
        "currentState": DRAFT,
        "stateEnteredAt": now,
        "events": [_event(DRAFT, actor, actor_type, reason, now)],
    }


def advance(
    payments,
    payment_oid,
    to_state: str,
    *,
    actor: str,
    reason: str,
    actor_type: str = "SERVICE",
    from_state: Optional[str] = None,
    session=None,
    extra: Optional[dict] = None,
) -> dict:
    """Move a persisted payment to `to_state`. Returns the updated document.

    `from_state` is the caller's expectation. When given it is enforced as a guard in the
    query itself, so a concurrent advance loses rather than silently double-transitioning.

    `extra` is merged into the `$set` — for stage-owned fields written at the same moment as
    the transition (`clearing.settledAt`, a fraud block), so the state change and the data it
    attests to land in one write.
    """
    if to_state not in TRANSITIONS:
        raise IllegalTransition(f"unknown state {to_state!r}")

    now = datetime.now(timezone.utc)
    query = {"_id": payment_oid}
    if from_state is not None:
        if to_state not in TRANSITIONS[from_state]:
            raise IllegalTransition(f"{from_state} -> {to_state} is not a legal transition")
        query["lifecycle.currentState"] = from_state

    update = {
        "$set": {
            "lifecycle.currentState": to_state,
            "lifecycle.stateEnteredAt": now,
            # The schema's mirror rule: `status` follows `currentState`, never leads it.
            "status": to_state,
            "updatedAt": now,
            **(extra or {}),
        },
        "$push": {"lifecycle.events": _event(to_state, actor, actor_type, reason, now)},
    }

    updated = payments.find_one_and_update(query, update, session=session, return_document=True)
    if updated is None:
        # Either the payment vanished or its state was not `from_state`. Both are bugs at
        # this layer — a lost race on a state transition is not a business outcome.
        raise IllegalTransition(
            f"could not advance {payment_oid} to {to_state}"
            + (f" from {from_state}" if from_state else "")
        )
    return updated


def reject(
    payments,
    payment_oid,
    *,
    reason: str,
    actor: str = "transactions-service",
    to_state: str = REJECTED,
    session=None,
) -> Optional[dict]:
    """Terminate a payment. Tolerant by design — never masks the original failure.

    Called by the saga when a stage raises, so it must not raise itself: the caller is
    already re-raising the real error, and a bookkeeping failure here would replace a useful
    business message with a confusing one.
    """
    try:
        return advance(
            payments, payment_oid, to_state,
            actor=actor, reason=reason, session=session,
        )
    except Exception:  # noqa: BLE001 - deliberate; see the docstring
        logger.exception("could not record %s for payment %s", to_state, payment_oid)
        return None


def _event(state: str, actor: str, actor_type: str, reason: str, at: datetime) -> dict:
    return {
        "state": state,
        "at": at,
        "actor": actor,
        "actorType": actor_type,
        "reason": reason,
    }


def advance_ctx(ctx, to_state: str, *, actor: str, reason: str, session=None, extra=None) -> dict:
    """`advance` for a stage holding a `PaymentContext`.

    Threads `from_state` from the context and keeps it in sync, so a stage does not repeat
    that bookkeeping. Duck-typed on purpose — this module imports nothing from `process/`,
    keeping the dependency pointing inward.
    """
    updated = advance(
        ctx.collections.payments,
        ctx.payment_oid,
        to_state,
        actor=actor,
        reason=reason,
        from_state=ctx.current_state,
        session=session,
        extra=extra,
    )
    ctx.current_state = to_state
    ctx.payment_doc = updated
    return updated
