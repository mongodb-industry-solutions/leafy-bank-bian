"""Inbound stage 9 — Unable to Apply (FR-9.IN1..4). Her L1331: the stage with the most
substantive new content for inbound.

UTA is *"the industry-standard term for an inbound payment that cannot be credited as
instructed"*. It has **no outbound equivalent**: outbound's stage-3 gate stops an unviable
payment before it is ever submitted, but an inbound payment has already arrived before
Leafy Bank can evaluate it (her L1350). So where outbound exceptions offer retry/return of a
payment *we* sent, UTA offers two structurally different actions on money *we hold*:

| action     | what happens                                                        |
|------------|---------------------------------------------------------------------|
| **Repair** | an operator corrects/confirms the beneficiary; the payment resumes   |
| **Return** | a pacs.004 goes back to the sender; the payment closes as `RETURNED` |

## Where a UTA comes from

Two places only (FR-9.IN1): a `NO_MATCH` at stage 2 (`resolve._fail`) and a `REJECT` at
stage 4 (`accept._reject`). Both are cases where the funds arrived and cannot be applied as
instructed — which is exactly the definition, and why they share one queue category.

## Why the payment is HELD, not REJECTED

A UTA payment stops mid-saga with no terminal state. That is deliberate: `REJECTED` would
close it, and the money would sit with nothing to resume and nothing to return it with.
Parking it keeps both operator actions available, which is the entire requirement.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bson import ObjectId

from contexts.payment_order_initiation.domain import checks, lifecycle
from process import exceptions as exc_module
from shared.refs import derive_ref

logger = logging.getLogger(__name__)

STAGE = "9 exceptions"

SERVICE = "financial-gateway"


def record(ctx, *, reason: str, stage: str) -> dict:
    """Open a UTA exception for the payment on `ctx`.

    Deliberately NOT `process.exceptions.record_exception`: that writer refuses any category
    outside `OUTGOING_CATEGORIES` by design (*"UTA (incoming) or a typo — neither is this
    stage's to write"*). The stub's enum has always carried `UTA` reserved for this path, so
    the document shape is unchanged — only the writer is inbound's own.

    Idempotent on the same `(paymentId, UTA, OPEN)` key the outbound writer uses, via the
    same unique partial index (`idx_exception_open_unique`), so a replayed message or a
    concurrent worker cannot open two.
    """
    payment_id = ctx.payment_id
    coll = ctx.collections.db["exceptions"]

    existing = coll.find_one({
        "paymentId": payment_id,
        "category": exc_module.CATEGORY_UTA,
        "status": exc_module.STATUS_OPEN,
    })
    if existing is not None:
        return existing

    now = datetime.now(timezone.utc)
    oid = ObjectId()
    resolution = (ctx.payment_doc or {}).get("beneficiaryResolution") or {}
    doc = {
        "_id": oid,
        "exceptionId": derive_ref("EXC", oid),
        "paymentId": payment_id,
        "category": exc_module.CATEGORY_UTA,
        "status": exc_module.STATUS_OPEN,
        "severity": exc_module.SEVERITY_ACTION_REQUIRED,
        "source": {"stage": stage, "service": SERVICE},
        "detail": {
            "reason": reason,
            # What the sender claimed, so the operator can see it without opening the
            # message — her demo panel prints exactly these two lines (L1341-1343).
            "claimedName": (ctx.claimed_creditor or {}).get("name"),
            "claimedAccountNo": (ctx.claimed_creditor or {}).get("accountNo"),
            "matchOutcome": resolution.get("matchOutcome"),
            "closestAccountId": resolution.get("matchedAccountId"),
            "amount": ctx.instructed_amount,
            "currency": ctx.instructed_currency,
        },
        "resolution": None,
        "agent": None,
        "createdAt": now,
        "updatedAt": now,
        "sourceSystem": SERVICE,
    }
    try:
        coll.insert_one(doc)
    except Exception as insert_error:  # DuplicateKeyError under the unique partial index
        existing = coll.find_one({
            "paymentId": payment_id,
            "category": exc_module.CATEGORY_UTA,
            "status": exc_module.STATUS_OPEN,
        })
        if existing is not None:
            return existing
        raise insert_error
    logger.info("UTA opened for %s: %s", payment_id, reason)
    return doc


def repair(service, exc: dict, payment: dict, *, matched_account_id: str,
           note: str = None) -> dict:
    """**Repair** — an operator confirms or corrects the beneficiary (FR-9.IN2).

    Her L1336: the operator *"manually corrects or confirms the beneficiary account (e.g.
    resolves a name variant, corrects a transposed digit) and the payment resumes at Stage
    3. Proceeds to credit posting (Stage 6) once resolved."*

    ## The correction is PERSISTED before the resume

    `beneficiaryResolution` is rewritten on the payment — `matchOutcome: MATCHED`,
    `matchMethod: MANUAL_REPAIR`, the operator's account choice — **and only then** does the
    saga re-enter. Carrying the correction on an in-memory context instead is defect
    2026-09-28 (`control-not-persisted-across-reentry`) exactly: the resume rebuilds its
    context from the document, so anything not written there is lost and the repair would
    silently un-apply.

    `MANUAL_REPAIR` as the method (never `EXACT`) is what keeps the record honest: a human
    confirming a beneficiary is different evidence from an algorithm matching a string, and
    an auditor must be able to tell which happened.

    ## Ordering (B5 + A4)

    Precondition first, then the claim, then the act, with a rollback if the act fails —
    the complete rule from defects 2026-09-23 B5 and 2026-09-28 A4.
    """
    from contexts.financial_gateway.domain import name_match

    now = datetime.now(timezone.utc)
    payments = service.payments

    # --- precondition, BEFORE the claim (A4) ---------------------------------
    state = (payment.get("lifecycle") or {}).get("currentState")
    if state != lifecycle.RECEIVED:
        raise ValueError(
            f"Payment {payment['paymentId']} is {state}, not {lifecycle.RECEIVED} — only a "
            f"payment held at receipt can be repaired. Exception {exc['exceptionId']} "
            "stays open."
        )

    account = service.db["accounts"].find_one({"accountId": matched_account_id})
    if account is None:
        raise ValueError(f"Account {matched_account_id} not found — cannot repair.")
    if account.get("status") != "ACTIVE":
        raise ValueError(
            f"Account {matched_account_id} is {account.get('status')}, not ACTIVE — "
            "cannot be credited."
        )

    # --- persist the correction BEFORE resuming (the whole point) ------------
    customer_id = (account.get("customerSnapshot") or {}).get("customerId")
    customer = service.db["customers"].find_one({"customerId": customer_id}) if customer_id else None
    payments.update_one(
        {"_id": payment["_id"]},
        {"$set": {
            "beneficiaryResolution": {
                "matchOutcome": name_match.MATCHED,
                "matchedAccountId": matched_account_id,
                "matchMethod": name_match.METHOD_MANUAL_REPAIR,
                "checkedAt": now,
                "repairedBy": "payments-operations",
                "repairNote": note,
            },
            "creditor.accountId": matched_account_id,
            "creditor.accountNo": account.get("accountNumber"),
            "customerId": customer_id,
            "updatedAt": now,
        }},
    )

    checks.append_checks(payments, payment["_id"], [
        checks.check(
            STAGE, "uta_repaired", checks.PASS, mode=checks.SYNC,
            detail=(
                f"Operator confirmed beneficiary account {matched_account_id} "
                f"({((customer or {}).get('identification') or {}).get('legalName')!r}) "
                f"for a payment that could not be applied automatically. Resuming."
            ),
            actor="payments-operations", at=now,
        )
    ])

    # The repair IS stage 2's resolution, performed by a human — so it must also make stage
    # 2's transition. Without this the payment resumes at stage 3 still sitting at RECEIVED
    # and the first `advance` raises `RECEIVED -> ENRICHED is not a legal transition`.
    # Advancing here (rather than loosening the state machine) keeps the invariant that a
    # payment reaches ENRICHED only through a completed beneficiary resolution, whether that
    # resolution was automatic or manual.
    lifecycle.advance(
        payments,
        payment["_id"],
        lifecycle.VALIDATED,
        actor="payments-operations",
        actor_type="HUMAN",
        reason=f"Beneficiary confirmed by operator repair ({matched_account_id})",
        from_state=lifecycle.RECEIVED,
    )

    # --- resume, and roll the claim back if it fails (A4) --------------------
    try:
        return service.resume_inbound(payment["paymentId"])
    except Exception:
        logger.warning(
            "uta.repair: resume failed for %s — exception %s stays open",
            payment["paymentId"], exc["exceptionId"], exc_info=True,
        )
        raise


def build_return(service, payment: dict, *, return_reason_code: str) -> dict:
    """**Return** — generate a pacs.004 and close the payment as `RETURNED` (FR-9.IN3).

    No Leafy Bank customer is credited (her L1337), so there is no money to move: the funds
    never left the clearing account, because an inbound payment that fails at stage 2 or 4
    never reached stage 6's posting. That is why this writes a message and a terminal state
    and nothing else — and why it is `RETURNED`, not `REJECTED` (DR-9.IN2: REJECTED means
    never accepted; RETURNED means accepted-then-returned).

    The terminal state depends on whether acceptance had already happened — `_close_returned`
    picks it, per DR-9.IN2.
    """
    from contexts.financial_gateway.domain.inbound_documents import return_doc

    now = datetime.now(timezone.utc)
    oid = ObjectId()

    original_ref = (payment.get("refs") or {}).get("canonicalJsonId")
    message = return_doc(
        oid=oid,
        payment=payment,
        return_reason_code=return_reason_code,
        original_message_ref=original_ref,
        now=now,
    )
    service.db["paymentMessages"].insert_one(message)

    _close_returned(service, payment, message, return_reason_code, now)
    return message


def _close_returned(service, payment: dict, message: dict, reason_code: str,
                    now: datetime) -> None:
    """Terminal state for a returned inbound payment — DR-9.IN2's distinction, in code.

    * returned **before** acceptance (held at `RECEIVED`): the payment was never accepted,
      so it closes `REJECTED`;
    * returned **after** acceptance (`ACCEPTED` or beyond): Leafy Bank told the sender it
      would apply the funds, so it closes `RETURNED`.

    Her DR-9.IN2 states the rule in exactly those terms, and `lifecycle._SIDE_STATE_RANK`
    already enforces which terminals each state can reach — this function picks the one the
    state machine allows rather than fighting it.
    """
    state = (payment.get("lifecycle") or {}).get("currentState")
    to_state = (
        lifecycle.RETURNED if state in (lifecycle.ACCEPTED, lifecycle.IN_PROGRESS)
        else lifecycle.REJECTED
    )
    lifecycle.advance(
        service.payments,
        payment["_id"],
        to_state,
        actor="payments-operations",
        actor_type="HUMAN",
        reason=(
            f"Returned to sender via pacs.004 ({reason_code}) — "
            f"message {message['paymentMessageId']}."
        ),
        from_state=state,
        extra={
            "clearing.returnCode": reason_code,
            "refs.returnMessageId": message["paymentMessageId"],
        },
    )
    checks.append_checks(service.payments, payment["_id"], [
        checks.check(
            STAGE, "uta_returned", checks.PASS, mode=checks.SYNC,
            detail=(
                f"pacs.004 PaymentReturn {message['paymentMessageId']} transmitted to the "
                f"sending bank ({reason_code}). No Leafy Bank customer was credited."
            ),
            actor="payments-operations", at=now,
        )
    ])
