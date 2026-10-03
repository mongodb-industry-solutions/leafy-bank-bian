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
Writes ctx: fraud; current_state -> AUTHORISED (-> APPROVED), or MANUAL_FRAUD_REVIEW on a
            REVIEW hold; `payments.fraud`, `payments.correspondent.sanctionsCheck`,
            `payments.clearing.authorisedAt` (APPROVED only), `payments.order`,
            `payments.confirmation` (APPROVED only — FR-4.4), `payments.refs.paymentOrderId`;
            seven `checks[]` entries

## Her demo display is the check list (L508-515)

    TRANSACTION AUTHORIZATION
    * Risk assessment completed (rules + model score)
    * Sanctions and AML screening passed
    * Fraud risk score within threshold
    Decision: APPROVED ✓

The four checks below are named so that screen renders straight off `checks[]` with no
bespoke mapping in the frontend.

## Three outcomes

`fraud.decision` is the spec's own enum — `APPROVED | REVIEW | DECLINED` — so all three were
already *legal*; they were simply unreachable behind a hardcoded literal.

* **APPROVED** — AUTHORISED, then APPROVED. The payment order is committed.
* **DECLINED** — raise. The saga marks the payment REJECTED (`payment_lifecycle._mark_rejected`)
  and re-raises, so stage 5 can never run on an unapproved payment. This activates code that
  sat commented out since the stage was scaffolded.
* **REVIEW** — advance to **MANUAL_FRAUD_REVIEW** and stop there via `ctx.stop()`. The payment
  never reaches AUTHORISED or APPROVED and **no payment order is written** — the bank has
  not committed. `clearing.authorisedAt` is not written either: a payment under manual
  review has not been authorised.

`MANUAL_FRAUD_REVIEW` is the manual-fraud-review state (FR-4.13 / DR-4.2). Originally added
2026-09-11 as `PENDING_REVIEW` (Q33); renamed to Doina's `MANUAL_FRAUD_REVIEW` per her DR-4
(Sep 15), which also adds it to the canonical `status` / `currentState` / `events[].state`
enums — so the conformance guard reverted to the spec alone (the `_ENUM_EXTENSIONS` admission
it used while unratified is retired). The operator resolve route
(`POST /TransactionAuthorization/Resolve`) advances MANUAL_FRAUD_REVIEW -> AUTHORISED ->
APPROVED (approve) or -> REJECTED (decline).

## The payment order lands here, not in 4a

`refs.paymentOrderId`'s own spec description settles the Q5 timing conflict: *"Written at
orchestration, after authorization."* `routingSnapshots` is 4a's; `payments.order` is the
bank's **commitment**, so it follows the decision (doc 18 B1). The commitment was folded
into `payments` from a separate `paymentOrders` collection per Doina's Aug 27 target model
(L427-429).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

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

    # --- operator manual-review override (FR-4.13 resume) -------------------
    # An operator approved or declined a payment held at MANUAL_FRAUD_REVIEW. The model's REVIEW
    # assessment and the sanctions result are already on the document from the hold, so this
    # branch does NOT re-score and does NOT re-screen — it commits the authorisation the model
    # withheld (APPROVED) or raises so the saga marks the payment REJECTED. `ctx.review_override`
    # is set only by `PaymentsService.resolve_review`, which re-enters the saga at this stage.
    if ctx.review_override == "APPROVED":
        persisted_fraud = payment.get("fraud") or {}
        persisted_sanctions = (payment.get("correspondent") or {}).get("sanctionsCheck") or {}
        recorded.append(checks.check(
            STAGE, "manual_review_approved", checks.PASS,
            detail=(
                f"Operator approved a payment held for manual review. Model score "
                f"{persisted_fraud.get('score')}/100 (decision REVIEW) overridden by "
                f"operator review."
            ),
            actor="operator-review", at=now,
        ))
        ctx.fraud = persisted_fraud
        _flush(ctx, recorded)
        _commit_approval(
            ctx, payment=payment, sanctions_block=persisted_sanctions, now=now,
            authorised_actor="operator-review",
            authorised_reason=(
                f"Operator manual-review approval; model score "
                f"{persisted_fraud.get('score')}/100 (decision REVIEW) overridden."
            ),
            approval_reason="Approved by operator manual review.",
            authorization={
                "decision": fraud_rules.APPROVED,
                "fraudScore": persisted_fraud.get("score"),
                "sanctionsStatus": persisted_sanctions.get("status"),
                "authorisedAt": now,
                "authorisedBy": "operator-review",
                "serviceDomain": "TransactionAuthorization",
                "reviewOverride": True,
            },
        )
        return

    if ctx.review_override == "REJECTED":
        recorded.append(checks.check(
            STAGE, "manual_review_declined", checks.FAIL,
            detail="Operator declined a payment held for manual review.",
            actor="operator-review", at=now,
        ))
        _flush(ctx, recorded)
        raise ValueError("Payment declined by operator manual review.")

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

    # --- 4. quote_reconfirmed (R17 / FR-4.12) -------------------------------
    # The fee AND the FX quote stage 3 established are re-derived from the same schedules and
    # compared immediately before execution. A *changed* quote refuses — the FR's "reject/
    # hold" — so the payment is rejected and the originator re-initiates for current pricing.
    # Re-pricing silently is the one option the FR does not offer. A *missing* quote is a
    # stage-3 gap (nothing to re-confirm), so it WARNs rather than refusing.
    _record_fee_reconfirmation(record, refuse, payment)
    _record_fx_reconfirmation(
        record, refuse, payment,
        debtor_account_currency=(ctx.debtor_account or {}).get("currency"),
    )

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

    if assessment.holds:
        # REVIEW — held at MANUAL_FRAUD_REVIEW, not AUTHORISED (FR-4.13). The bank has NOT
        # authorised a payment still under manual review, so `clearing.authorisedAt` is not
        # written and no order is committed (`payments.order` stays null). The fraud block
        # and screening result land in the same write as the transition — the score and the
        # state that attests to it are one fact (§11). Queryable on `status ==
        # MANUAL_FRAUD_REVIEW` and visible on the operations dashboard. An operator's later
        # approve resumes MANUAL_FRAUD_REVIEW -> AUTHORISED -> APPROVED (transition wired; the
        # resume route itself is a follow-on, not built here).
        lifecycle.advance_ctx(
            ctx, lifecycle.MANUAL_FRAUD_REVIEW,
            actor="fraud-service",
            reason=(
                f"Fraud score {assessment.score}/100, decision {assessment.decision}; "
                f"sanctions {screening.status} — held for manual review"
            ),
            extra={
                "fraud": ctx.fraud,
                "correspondent.sanctionsCheck": sanctions_block,
            },
        )
        ctx.stop(ctx.payment_doc)
        return

    # The fraud block, the screening result and the authorisation timestamp land in the SAME
    # write as the AUTHORISED transition — the score and the state that attests to it are one
    # fact, and splitting them would let a reader see one without the other (§11). The
    # commitment (order + confirmation) lands in the SAME write as APPROVED, for the same
    # reason. Both are in `_commit_approval`, shared with the operator-review override above.
    _commit_approval(
        ctx, payment=payment, sanctions_block=sanctions_block, now=now,
        authorised_actor="fraud-service",
        authorised_reason=(
            f"Fraud score {assessment.score}/100, decision {assessment.decision}; "
            f"sanctions {screening.status}"
        ),
        approval_reason=_approval_reason(ctx),
        authorization={
            "decision": assessment.decision,
            "fraudScore": assessment.score,
            "sanctionsStatus": screening.status,
            "authorisedAt": now,
            "authorisedBy": "fraud-service",
            "serviceDomain": "TransactionAuthorization",
        },
    )


# --------------------------------------------------------------------------- #

def _commit_approval(
    ctx, *, payment, sanctions_block, now, authorised_actor, authorised_reason,
    approval_reason, authorization,
):
    """The AUTHORISED -> APPROVED transition + the commitment it attests to.

    Shared by the normal fraud-APPROVED path and the operator-review override. The fraud
    block, the screening result and `clearing.authorisedAt` land in the AUTHORISED write; the
    `order` sub-doc, the `confirmation` sub-doc and `refs.paymentOrderId` land in the APPROVED
    write — each state change carries the facts it attests to in one update (§11). The
    `originator_confirmed` check is appended after the commitment exists, ASYNC because a
    confirmation is a notification, not an inline answer (FR-4.4).

    `authorization` is the record stamped on `order.authorization` — the normal path writes the
    model's decision; the override writes the operator's, with `reviewOverride: true`.
    """
    lifecycle.advance_ctx(
        ctx, lifecycle.AUTHORISED,
        actor=authorised_actor,
        reason=authorised_reason,
        extra={
            "fraud": ctx.fraud,
            "correspondent.sanctionsCheck": sanctions_block,
            "clearing.authorisedAt": now,
        },
    )

    # Folded into `payments.order` per Doina's Aug 27 target model (L427-429): the
    # `paymentOrders` collection is struck through; the commitment lives as a sub-document.
    order = documents.payment_order(
        payment=ctx.payment_doc or payment,
        strategy=ctx.execution_strategy,
        routing_snapshot_id=ctx.routing_snapshot_id,
        authorization=authorization,
        now=now,
        warehoused=ctx.warehoused,
    )

    # FR-4.4 — the originator confirmation (BIAN PaymentConfirmation, SD 47766). Distinct from
    # stage 5's `notifications`: this says "on track, execution path committed," earlier and
    # independent of settlement.
    confirmation = documents.confirmation(order=order, now=now)

    lifecycle.advance_ctx(
        ctx, lifecycle.APPROVED,
        actor="transactions-service",
        reason=approval_reason,
        extra={
            "order": order,
            "confirmation": confirmation,
            "refs.paymentOrderId": order["paymentOrderId"],
        },
    )

    checks.append_checks(ctx.collections.payments, ctx.payment_oid, [
        checks.check(
            STAGE, "originator_confirmed", checks.PASS, mode=checks.ASYNC,
            actor="payment-confirmation-service",
            at=now,
            detail=(
                f"Confirmation {confirmation['confirmationId']} sent to the originator via "
                f"{confirmation['channel']} — execution path committed as "
                f"{order['paymentOrderId']} ({order['executionStrategy']}"
                + (f" via {order['clearingNetwork']}" if order["clearingNetwork"] else "")
                + f", value date {order['valueDate']}). Distinct from the settlement "
                "confirmation that follows much later."
            ),
        )
    ])


def _record_fee_reconfirmation(record, refuse, payment: dict) -> None:
    """R17 / FR-4.12 — re-derive the fee from the same schedule stage 3 used and compare.

    A *changed* fee refuses (the FR's "reject/hold"): the payment is rejected and the
    originator re-initiates to obtain current pricing. Re-pricing silently is the one option
    the FR does not offer. A *missing* fee is a stage-3 gap, not quote drift — there is
    nothing to re-confirm — so it WARNs rather than refusing.

    The fee is a flat demo constant, so in the synchronous saga this guard can only ever
    PASS. It exists to catch drift, tampering or a future priced schedule whose quote can
    change between enrichment and authorization — which is what "re-validate the quote
    immediately before execution" means. The earlier "re-confirming a mock is theatre"
    deferral was wrong in shape: a guard that asserts a stored value still matches its
    source is a real guard even when the source is constant. The FX half applies the same
    reasoning (see `_record_fx_reconfirmation`).
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
            "A wire carries no fee record — stage 3 enrichment did not price it. "
            "Nothing to re-confirm.",
        )
        return

    expected = enrichment_plan.WIRE_FEE
    actual = fees[0].get("amount")
    fee_type = fees[0].get("type")
    if actual != expected:
        refuse(
            "fees_reconfirmed",
            f"Fee changed since enrichment: {actual} on the payment, {expected:,.2f} in "
            "the current schedule. Payment rejected — re-initiate to obtain current "
            "pricing. The settlement amount was never adjusted (doc 17 §7).",
        )
    record(
        "fees_reconfirmed", checks.PASS,
        f"{expected:,.2f} {fees[0].get('currency')} {fee_type} still matches the fee "
        f"schedule, borne by {fees[0].get('chargedTo')}. The settlement amount is "
        "unchanged.",
    )


def _record_fx_reconfirmation(record, refuse, payment: dict, *,
                              debtor_account_currency: Optional[str]) -> None:
    """R17 / FR-4.12 — re-confirm the FX quote stage 3 attached (FR-3.14).

    Re-derives the expected simulated rate for the (instructed → debtor-account) pair from
    the same table `enrichment_plan._plan_fx` used and compares it to `payments.fxRate`.
    A *changed* rate refuses (reject/hold): re-initiate for a current quote. A currency
    mismatch with no `fxRate` is a stage-3 gap → WARN. No mismatch → SKIP.

    The rate is a fixed mock, so like the fee this guard can only PASS in the synchronous
    saga; it exists to catch drift, tampering or a future live FX provider whose quote can
    expire — which is the "expired or changed" case the FR names. That is what makes it a
    real guard rather than theatre: it asserts the stored quote still matches its source.
    """
    from contexts.payment_order_initiation.domain import enrichment_plan

    instructed_currency = payment.get("instructedCurrency")
    if (not debtor_account_currency
            or debtor_account_currency == instructed_currency):
        record(
            "fx_quote_reconfirmed", checks.SKIP,
            f"No FX required — instructed currency {instructed_currency} "
            f"matches debtor account currency {debtor_account_currency}.",
        )
        return

    expected = enrichment_plan.SIMULATED_FX_RATES.get(
        (instructed_currency, debtor_account_currency),
        enrichment_plan._DEFAULT_SIMULATED_RATE,
    )
    actual = payment.get("fxRate")
    if actual is None:
        record(
            "fx_quote_reconfirmed", checks.WARN,
            f"Instructed {instructed_currency} vs debtor {debtor_account_currency} "
            "differs, but stage 3 attached no fxRate — nothing to re-confirm.",
        )
        return
    if actual != expected:
        refuse(
            "fx_quote_reconfirmed",
            f"FX quote changed since enrichment: {actual} on the payment, {expected} in "
            "the current schedule. Payment rejected — re-initiate to obtain a current "
            "quote.",
        )
    record(
        "fx_quote_reconfirmed", checks.PASS,
        f"Simulated FX rate {actual} ({instructed_currency}→"
        f"{debtor_account_currency}) still matches the rate schedule. SIMULATED — "
        "no live FX provider in Phase 1.",
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
