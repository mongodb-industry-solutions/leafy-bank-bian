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

from shared import business_clock, stage_events as stage_event_log


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
# Inbound entry state (DR-1.IN2). The peer of INITIATED, not a replacement: her L188 —
# "parallel in role to INITIATED, but reflects passive receipt rather than active customer
# submission." An inbound payment is born at DRAFT like any other, advances to RECEIVED
# instead of INITIATED, and rejoins the shared path at VALIDATED. That is why it is a side
# state off HAPPY_PATH rather than a member: adding it to the list would shift every index
# after it, and `_IN_FLIGHT_FROM` is computed from those indices.
RECEIVED = "RECEIVED"
VALIDATED = "VALIDATED"
ENRICHED = "ENRICHED"
FINAL_VALIDATED = "FINAL_VALIDATED"
ROUTED = "ROUTED"
AUTHORISED = "AUTHORISED"
APPROVED = "APPROVED"
# A REVIEW fraud decision (stage 4b) diverts here from ROUTED instead of advancing to
# AUTHORISED — the bank has not authorised a payment still under manual review (FR-4.13 /
# DR-4.2). Originally added 2026-09-11 as `PENDING_REVIEW` (Q33); renamed to Doina's own
# `MANUAL_FRAUD_REVIEW` per her DR-4.2 (Sep 15), which also adds it to the canonical
# `status` / `currentState` / `events[].state` enums — so the conformance guard no longer
# needs the `_ENUM_EXTENSIONS` admission it used while the value was unratified.
MANUAL_FRAUD_REVIEW = "MANUAL_FRAUD_REVIEW"
SUBMITTED = "SUBMITTED"
# Inbound peer of SUBMITTED (DR-5.IN1), set when the positive pacs.002 status report is
# transmitted back to the sending bank (FR-5.IN3). Like RECEIVED, a side state off
# HAPPY_PATH — but unlike RECEIVED it sits AFTER the in-flight boundary, so its legal
# terminals are the post-execution ones (a payment accepted and then returned closes as
# RETURNED, never REJECTED — DR-9.IN2 draws exactly that line). `_SIDE_STATE_RANK` is what
# tells `_terminals_for` so.
ACCEPTED = "ACCEPTED"
IN_PROGRESS = "IN_PROGRESS"
POSTED = "POSTED"
SETTLED = "SETTLED"
RECONCILED = "RECONCILED"

# Cutoff plan A2: four holds, reachable only by a tagged (demo-clock) payment. Side states off
# HAPPY_PATH for the same reason as MANUAL_FRAUD_REVIEW. Each waits for a human decision
# (`PaymentsService.approve_payment` / `recheck_funds` / `resolve_screening` /
# `decide_cutoff`); none has moved money, so all four rank before SUBMITTED.
PENDING_APPROVAL = "PENDING_APPROVAL"      # stage 2: a second signatory must approve
PENDING_FUNDS = "PENDING_FUNDS"            # stage 3: a short wire waits for funds
CUTOFF_EXCEPTION = "CUTOFF_EXCEPTION"      # stage 4a: past the internal cut-off
PENDING_SCREENING = "PENDING_SCREENING"    # stage 4b: a potential sanctions match

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
# MANUAL_FRAUD_REVIEW is a side state off the happy path: a REVIEW decision (stage 4b) diverts
# ROUTED -> MANUAL_FRAUD_REVIEW instead of ROUTED -> AUTHORISED. An operator's later approve
# resumes MANUAL_FRAUD_REVIEW -> AUTHORISED -> APPROVED; a manual decline sends it to REJECTED
# (a pre-execution terminal — no money has moved). Not in HAPPY_PATH on purpose, so the
# happy-path ordering and the _IN_FLIGHT_FROM index stay unchanged.
_FORWARD[ROUTED] = {AUTHORISED, MANUAL_FRAUD_REVIEW}
_FORWARD[MANUAL_FRAUD_REVIEW] = {AUTHORISED}

# --- the inbound path (incoming wire) ----------------------------------------
# Two side states carry an inbound payment; everything between them is the SHARED path.
#
#   DRAFT -> RECEIVED -> VALIDATED -> ENRICHED -> FINAL_VALIDATED -> ACCEPTED -> IN_PROGRESS
#            \_ stage 1                \_ stages 2/3, unchanged      \_ stages 4/5   \_ 6/7/8
#
# RECEIVED replaces INITIATED (passive receipt, not customer submission) and ACCEPTED
# replaces the ROUTED/AUTHORISED/APPROVED/SUBMITTED run — her L823: inbound has no
# rail-selection or execution-path decision, only a binary accept/reject, so those four
# outbound states have no inbound meaning. From IN_PROGRESS on, the two directions share
# one path: stage 6 posting, stage 7 settlement and stage 8 reconciliation are the same
# machinery with the legs mirrored (FR-6.IN1 / FR-7.IN1 / FR-8.IN1).
_FORWARD[DRAFT] = {INITIATED, RECEIVED}
_FORWARD[RECEIVED] = {VALIDATED}
_FORWARD[FINAL_VALIDATED] = {ROUTED, ACCEPTED}
_FORWARD[ACCEPTED] = {IN_PROGRESS}

# POSTED is stamped by the ledger service, asynchronously, and may arrive after SETTLED —
# the posting axis is independent (D1). Allow IN_PROGRESS -> SETTLED to skip it.
_FORWARD[IN_PROGRESS] = {POSTED, SETTLED}

# --- the cutoff holds (tagged payments only) ---------------------------------
# A held payment resumes onto the path it left. PENDING_APPROVAL can still fall short on
# funds when validation re-runs; PENDING_SCREENING can still divert to manual fraud review,
# or to CUTOFF_EXCEPTION when the analyst clears it after the internal cut-off. The
# PENDING_SCREENING -> ROUTED edge is the warehouse of a screening hold whose next value date
# was already chosen (HoldNextValueDate): ROUTED is where every warehoused payment waits.
_FORWARD[INITIATED] = {VALIDATED, PENDING_APPROVAL, PENDING_FUNDS}
_FORWARD[PENDING_APPROVAL] = {VALIDATED, PENDING_FUNDS}
_FORWARD[PENDING_FUNDS] = {VALIDATED}
_FORWARD[FINAL_VALIDATED] = {ROUTED, ACCEPTED, CUTOFF_EXCEPTION}
_FORWARD[ROUTED] = {AUTHORISED, MANUAL_FRAUD_REVIEW, PENDING_SCREENING}
_FORWARD[PENDING_SCREENING] = {AUTHORISED, MANUAL_FRAUD_REVIEW, CUTOFF_EXCEPTION, ROUTED}
_FORWARD[CUTOFF_EXCEPTION] = {ROUTED}


# Where each side state sits relative to the in-flight boundary, expressed as the happy-path
# state it stands in for. A side state is not IN HAPPY_PATH (adding it would shift every
# later index and `_IN_FLIGHT_FROM` with them), so it cannot be ranked by position — this
# map is that rank, and `_terminals_for` reads it instead of defaulting every side state to
# "pre-execution", which was true while MANUAL_FRAUD_REVIEW was the only one.
#
# ⚠️ ACCEPTED is the reason this map exists. It sits AFTER the boundary: by the time the
# pacs.002 is away, Leafy Bank has told the sender it will apply the funds. Left on the old
# default it would have been eligible for REJECTED, and DR-9.IN2 is explicit that a payment
# which was accepted and later returned closes as RETURNED — REJECTED means never accepted.
_SIDE_STATE_RANK = {
    MANUAL_FRAUD_REVIEW: ROUTED,     # before SUBMITTED — no money has moved
    RECEIVED: INITIATED,             # inbound entry, peer of INITIATED
    ACCEPTED: SUBMITTED,             # inbound peer of SUBMITTED — in flight
    PENDING_APPROVAL: INITIATED,     # the cutoff holds: none has moved money
    PENDING_FUNDS: INITIATED,
    CUTOFF_EXCEPTION: FINAL_VALIDATED,
    PENDING_SCREENING: ROUTED,
}


def _terminals_for(state: str) -> frozenset:
    if state not in HAPPY_PATH:
        # A side state is ranked by the happy-path state it stands in for. Unknown side
        # states keep the old conservative default (pre-execution).
        stand_in = _SIDE_STATE_RANK.get(state)
        if stand_in is None:
            return _PRE_EXECUTION_TERMINALS
        state = stand_in
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
    at: Optional[datetime] = None,
    stage_events=None,
) -> dict:
    """Move a persisted payment to `to_state`. Returns the updated document.

    `from_state` is the caller's expectation. When given it is enforced as a guard in the
    query itself, so a concurrent advance loses rather than silently double-transitioning.

    `extra` is merged into the `$set` — for stage-owned fields written at the same moment as
    the transition (`clearing.settledAt`, a fraud block), so the state change and the data it
    attests to land in one write.

    `at` is the transition time; None means real UTC. `advance_ctx` passes business time, so
    a demo-clock payment's events read on the same clock as its checks.

    `stage_events` is the `paymentStageEvents` handle; when given, a best-effort duration
    point is appended after the transition lands. Callers pass it for tagged payments only.
    """
    if to_state not in TRANSITIONS:
        raise IllegalTransition(f"unknown state {to_state!r}")

    now = at or datetime.now(timezone.utc)
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
    if stage_events is not None:
        stage_event_log.append(stage_events, updated, at=now)
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

    Stage events: tagged payments only, and never inside a session — time-series writes do
    not belong in a money-moving transaction.
    """
    db = getattr(ctx.collections, "db", None)
    events = (db[stage_event_log.STAGE_EVENTS]
              if getattr(ctx, "clock_run_id", None) and session is None and db is not None else None)
    updated = advance(
        ctx.collections.payments,
        ctx.payment_oid,
        to_state,
        actor=actor,
        reason=reason,
        from_state=ctx.current_state,
        session=session,
        extra=extra,
        at=business_clock.ctx_now(ctx),
        stage_events=events,
    )
    ctx.current_state = to_state
    ctx.payment_doc = updated
    return updated
