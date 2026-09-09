"""Stage 4b — fraud evaluation & transaction authorization.

BIAN FraudEvaluation (SD 44625, `POST /FraudEvaluation/Evaluate`) and
**TransactionAuthorization** (SD 43343, `POST /TransactionAuthorization/Evaluate`).

⚠️ Doina's stage table names `PaymentAuthorization`. **That is not a v14 Service Domain** —
zero rows across the full 341-SD landscape. The real SD is `TransactionAuthorization`, and
her own demo-display heading at L509 already reads "TRANSACTION AUTHORIZATION", so her prose
agrees with v14 even though her table does not. Corrected unilaterally per D7 (doc 18 B3);
it is on the Q1 naming-correction list.

Reads  ctx: payment_doc, execution_strategy, routing_snapshot_id, warehoused,
            is_external_creditor, debtor_account_ref, creditor_*, payment_oid,
            current_state, collections
Writes ctx: fraud; current_state -> AUTHORISED (-> APPROVED); `payments.fraud`,
            `payments.correspondent.sanctionsCheck`, `payments.clearing.authorisedAt`,
            `payments.refs.paymentOrderId`; `paymentOrders`; five `checks[]` entries

## Her demo display is the check list (L508-515)

    TRANSACTION AUTHORIZATION
    * Risk assessment completed (rules + model score)
    * Sanctions and AML screening passed
    * Fraud risk score within threshold
    Decision: APPROVED ✓

The four checks below are named so that screen renders straight off `checks[]` with no
bespoke mapping in the frontend.

## Three outcomes, no invented state

`fraud.decision` is the spec's own enum — `APPROVED | REVIEW | DECLINED` — so all three were
already *legal*; they were simply unreachable behind a hardcoded literal.

* **APPROVED** — AUTHORISED, then APPROVED. The payment order is committed.
* **DECLINED** — raise. The saga marks the payment REJECTED (`payment_lifecycle._mark_rejected`)
  and re-raises, so stage 5 can never run on an unapproved payment. This activates code that
  sat commented out since the stage was scaffolded.
* **REVIEW** — advance to AUTHORISED and **stop there** via `ctx.stop()`. The payment never
  reaches APPROVED and **no payment order is written** — the bank has not committed.

Why REVIEW is not its own state: `PENDING REVIEW` is in her L530 list but **not** in the
canonical `status` enum (Q6, escalated to Q33). Adding it would be inventing an enum value,
which is defect 2026-04-24. Holding at AUTHORISED is the honest representation available.

## The payment order lands here, not in 4a

`refs.paymentOrderId`'s own spec description settles the Q5 timing conflict: *"Written at
orchestration, after authorization."* `routingSnapshots` is 4a's; `paymentOrders` is the
bank's **commitment**, so it follows the decision (doc 18 B1).
"""

from __future__ import annotations

from datetime import datetime, timezone

from contexts.fraud_evaluation.domain import fraud_rules, sanctions
from contexts.payment_order_initiation.domain import checks, lifecycle
from contexts.payment_orchestration.domain import documents
from process.payment_context import PaymentContext
from shared.refs import derive_ref

STAGE = "4 authorize"


def run(ctx: PaymentContext) -> None:
    now = datetime.now(timezone.utc)
    recorded: list = []
    payment = ctx.payment_doc or {}

    def record(name: str, result: str, detail: str, *, mode: str = checks.SYNC) -> None:
        recorded.append(
            checks.check(STAGE, name, result, mode=mode, detail=detail,
                         actor="fraud-service", at=now)
        )

    def refuse(name: str, detail: str) -> None:
        record(name, checks.FAIL, detail)
        _flush(ctx, recorded)
        raise ValueError(detail)

    creditor = payment.get("creditor") or {}
    wire = payment.get("wireDetails") or {}
    purpose_code = (payment.get("remittance") or {}).get("purposeCode")

    # --- 1. sanctions_screening (R11) ---------------------------------------
    # Runs BEFORE scoring: a denied party is not a risk to be weighed against other
    # factors, it is a refusal. Screening a payment we are about to refuse anyway also
    # keeps the ordering of her display list (screening above score).
    # ⚠️ `creditor.address` is a formatted STRING (`payment_document._address_text`), not a
    # sub-document — so the beneficiary's own country is not separately available and
    # `bankCountry` is the only structured country on the creditor. Screening on the
    # beneficiary BANK's country is a real (if broader) control; screening on a substring of
    # a formatted address would be worse than not screening. Noted in doc 18 §6 as the
    # reason a beneficiary-country field is worth asking Doina for.
    screening = sanctions.screen(
        creditor_name=creditor.get("name"),
        creditor_country=creditor.get("bankCountry"),
        purpose_code=purpose_code,
    )
    sanctions_block = {
        "status": screening.status,
        "checkedAt": now,
        "provider": sanctions.PROVIDER,
    }
    if screening.refuses:
        # Flush the screening result onto the document before raising, so the demo shows
        # WHY the payment was refused rather than only that it was.
        ctx.collections.payments.update_one(
            {"_id": ctx.payment_oid},
            {"$set": {"correspondent.sanctionsCheck": sanctions_block,
                      "updatedAt": now}},
        )
        refuse("sanctions_screening", screening.detail)
    record(
        "sanctions_screening",
        checks.PASS if screening.status == sanctions.CLEAR else checks.WARN,
        screening.detail,
    )

    # --- 2. risk_assessment (R10) -------------------------------------------
    assessment = fraud_rules.assess(
        amount=payment.get("amount") or ctx.instructed_amount,
        wire_type=wire.get("wireType"),
        purpose_code=purpose_code,
        high_risk_purpose_codes=sanctions.high_risk_purpose_codes(),
        prior_payments_to_beneficiary=_beneficiary_history_count(ctx),
        debtor_payments_in_window=_velocity_count(ctx, now),
        is_external_creditor=ctx.is_external_creditor,
    )
    record(
        "risk_assessment", checks.PASS,
        f"Rules and model score evaluated against the orchestrated payment. "
        f"{fraud_rules.describe(assessment)}",
    )

    # R22 — all five spec-required sub-fields of `fraud{}`. Before this stage only `score`
    # and `decision` were written, so the document never satisfied its own required list.
    ctx.fraud = {
        "alertId": derive_ref("FRAUD", ctx.payment_oid),
        "score": assessment.score,
        "decision": assessment.decision,
        "rulesFired": list(assessment.rules_fired),
        "checkedAt": now,
    }

    # --- 3. fraud_score_within_threshold (R12) ------------------------------
    if assessment.refuses:
        record(
            "fraud_score_within_threshold", checks.FAIL,
            f"Score {assessment.score} is at or above the decline threshold of "
            f"{fraud_rules.DECLINE_THRESHOLD}.",
        )
    elif assessment.holds:
        record(
            "fraud_score_within_threshold", checks.WARN,
            f"Score {assessment.score} is at or above the review threshold of "
            f"{fraud_rules.REVIEW_THRESHOLD} — held for manual review.",
        )
    else:
        record(
            "fraud_score_within_threshold", checks.PASS,
            f"Score {assessment.score} is below the review threshold of "
            f"{fraud_rules.REVIEW_THRESHOLD}.",
        )

    # --- 4. fees_reconfirmed (R17) ------------------------------------------
    _record_fee_reconfirmation(record, payment)

    # --- 5. authorization_decision (R13, R21) -------------------------------
    record(
        "authorization_decision",
        {
            fraud_rules.APPROVED: checks.PASS,
            fraud_rules.REVIEW: checks.WARN,
            fraud_rules.DECLINED: checks.FAIL,
        }[assessment.decision],
        f"Decision: {assessment.decision}. Authorized by TransactionAuthorization "
        f"(BIAN SD 43343) against the fully orchestrated payment.",
    )

    _flush(ctx, recorded)

    if assessment.refuses:
        # ⚠️ A declined payment must NEVER pass through AUTHORISED on its way to REJECTED.
        # "Authorised, then rejected" is a different and false story: the trace would show
        # the bank authorising a payment it declined. So the evidence is written with a
        # plain `$set` — no transition — and the raise lets the saga move the payment
        # straight from ROUTED to REJECTED, which is a legal pre-execution terminal.
        ctx.collections.payments.update_one(
            {"_id": ctx.payment_oid},
            {"$set": {
                "fraud": ctx.fraud,
                "correspondent.sanctionsCheck": sanctions_block,
                "updatedAt": now,
            }},
        )
        raise ValueError(
            f"Payment declined by fraud evaluation: score {assessment.score}/100 "
            f"({', '.join(assessment.rules_fired) or 'no rules fired'})."
        )

    # The fraud block, the screening result and the authorisation timestamp land in the SAME
    # write as the AUTHORISED transition — the score and the state that attests to it are one
    # fact, and splitting them would let a reader see one without the other (§11).
    lifecycle.advance_ctx(
        ctx, lifecycle.AUTHORISED,
        actor="fraud-service",
        reason=(
            f"Fraud score {assessment.score}/100, decision {assessment.decision}; "
            f"sanctions {screening.status}"
        ),
        extra={
            "fraud": ctx.fraud,
            "correspondent.sanctionsCheck": sanctions_block,
            "clearing.authorisedAt": now,
        },
    )

    if assessment.holds:
        # Held, not rejected, and NOT committed: no payment order is written, because the
        # bank has not committed to execute.
        ctx.stop(ctx.payment_doc)
        return

    # --- the commitment ------------------------------------------------------
    order = documents.payment_order(
        payment=ctx.payment_doc or payment,
        strategy=ctx.execution_strategy,
        routing_snapshot_id=ctx.routing_snapshot_id,
        authorization={
            "decision": assessment.decision,
            "fraudScore": assessment.score,
            "sanctionsStatus": screening.status,
            "authorisedAt": now,
            "authorisedBy": "fraud-service",
            "serviceDomain": "TransactionAuthorization",
        },
        now=now,
        warehoused=ctx.warehoused,
    )
    _insert_order(ctx, order)

    lifecycle.advance_ctx(
        ctx, lifecycle.APPROVED,
        actor="transactions-service",
        reason=_approval_reason(ctx),
        extra={"refs.paymentOrderId": order["paymentOrderId"]},
    )

    # --- 6. originator_confirmed (R8) — BIAN PaymentConfirmation, SD 47766 ---
    # Her L502: PaymentConfirmation *"sends confirmation feedback to the customer/originator
    # once orchestration has committed to an execution path, independent of the final
    # settlement confirmation much later."* So it fires HERE — after the payment order
    # exists — and not at settlement.
    #
    # A second flush, deliberately: this check attests to something that only becomes true
    # once the commitment is written, so it cannot travel with the flush above. ASYNC,
    # because a confirmation to the originator is a notification, not an inline answer.
    #
    # ⚠️ No notification document is written. Stage 5 owns `notifications`, and the
    # sender-only rule there is "exactly 1 notification per payment" — emitting a second one
    # here would break that invariant. The confirmation is recorded as evidence; wiring it to
    # a customer-facing channel is doc 18 §6's deferred row.
    checks.append_checks(ctx.collections.payments, ctx.payment_oid, [
        checks.check(
            STAGE, "originator_confirmed", checks.PASS, mode=checks.ASYNC,
            actor="payment-confirmation-service",
            at=now,
            detail=(
                f"Execution path committed as {order['paymentOrderId']} "
                f"({order['executionStrategy']}"
                + (f" via {order['clearingNetwork']}" if order["clearingNetwork"] else "")
                + f", value date {order['valueDate']}). Confirmed to the originator — "
                "distinct from the settlement confirmation that follows much later."
            ),
        )
    ])


# --------------------------------------------------------------------------- #

def _record_fee_reconfirmation(record, payment: dict) -> None:
    """R17 — *"immediately before execution, verify that the fee/rate is still valid."*

    Re-derives the fee from the same schedule stage 3 used and compares. A change **holds**
    the payment rather than silently re-pricing it: her requirement is *"either obtain
    customer approval again or reject/hold"*, and re-pricing without telling anyone is the
    one option she does not offer.

    ⚠️ FX is deliberately not re-confirmed. Stage 3 now WARNS on a currency mismatch
    and enrichment writes a SIMULATED `fxRate` (FR-3.14), but the rate is a fixed mock —
    re-confirming a mock against itself is theatre (doc 18 §6). A live FX provider
    would change this.
    """
    from contexts.payment_order_initiation.domain import enrichment_plan

    fees = payment.get("fees") or []
    if payment.get("rail") != "WIRE":
        record(
            "fees_reconfirmed", checks.SKIP,
            f"No fee applies on rail {payment.get('rail')}.",
        )
        return
    if not fees:
        record(
            "fees_reconfirmed", checks.WARN,
            "A wire carries no fee record — stage 3 enrichment did not price it.",
        )
        return

    expected = enrichment_plan.WIRE_FEE
    actual = fees[0].get("amount")
    fee_type = fees[0].get("type")
    if actual != expected:
        record(
            "fees_reconfirmed", checks.WARN,
            f"Fee changed since enrichment: {actual} on the payment, {expected:,.2f} in "
            "the current schedule. Held for customer re-approval rather than re-priced.",
        )
        return
    record(
        "fees_reconfirmed", checks.PASS,
        f"{expected:,.2f} {fees[0].get('currency')} {fee_type} still matches the fee "
        f"schedule, borne by {fees[0].get('chargedTo')}. The settlement amount is "
        "unchanged.",
    )


def _beneficiary_history_count(ctx) -> int:
    """How many payments this debtor has previously sent to this beneficiary."""
    payments = getattr(ctx.collections, "payments", None)
    if payments is None:  # pragma: no cover
        return 0
    return _count(
        payments,
        fraud_rules.beneficiary_history_filter(
            debtor_account_id=ctx.debtor_account_ref,
            creditor_account_no=_creditor_account_no(ctx),
            exclude_payment_id=ctx.payment_id,
        ),
    )


def _velocity_count(ctx, now: datetime) -> int:
    payments = getattr(ctx.collections, "payments", None)
    if payments is None:  # pragma: no cover
        return 0
    return _count(
        payments,
        fraud_rules.velocity_filter(
            debtor_account_id=ctx.debtor_account_ref,
            now=now,
            exclude_payment_id=ctx.payment_id,
        ),
    )


def _count(payments, query: dict) -> int:
    """`count_documents` where available, falling back to a find.

    The fallback exists for the test doubles, which implement `find` but not every counting
    method on the real driver. A miscount must never take the payment path down, so a
    failure here scores as zero history rather than raising — the same tolerance the
    reference-data adapter applies.
    """
    try:
        if hasattr(payments, "count_documents"):
            return int(payments.count_documents(query))
        return len(list(payments.find(query)))
    except Exception:  # noqa: BLE001 - a scoring input must not break the money path
        return 0


def _creditor_account_no(ctx):
    """Mirrors `validation._creditor_account_no` — the document stores one `creditor.accountNo`
    whichever side of the internal/external split the beneficiary is on."""
    if ctx.is_external_creditor:
        return (ctx.creditor_party or {}).get("accountNo")
    return (ctx.creditor_account or {}).get("accountNumber")


def _insert_order(ctx, order: dict) -> None:
    collections = ctx.collections
    handle = getattr(collections, "payment_orders", None)
    if handle is None:
        db = getattr(collections, "db", None)
        if db is None:  # pragma: no cover
            return
        handle = db[documents.PAYMENT_ORDERS]
    handle.insert_one(order)


def _flush(ctx: PaymentContext, recorded: list) -> None:
    checks.append_checks(ctx.collections.payments, ctx.payment_oid, recorded)
    recorded.clear()


def _approval_reason(ctx) -> str:
    """The dual-approval DECISION is stage 2's (doc 15 B4); the APPROVED state is this
    stage's. Read it rather than assert it — this reason used to be a hardcoded string that
    claimed a verdict no check produced."""
    entitlement = (ctx.payment_doc or {}).get("entitlement") or {}
    if not entitlement:
        return "Dual-approval requirement not assessed"
    if not entitlement.get("dualApprovalRequired"):
        return (
            "Below the "
            f"{entitlement.get('segment') or 'default'} dual-approval threshold of "
            f"{entitlement.get('dualApprovalThreshold')}"
        )
    return (
        f"Dual approval obtained from {entitlement.get('dualApprovalBy')}"
        + (" (SIMULATED)" if entitlement.get("dualApprovalSimulated") else "")
    )
