"""Stage 4b — fraud evaluation & transaction authorization.

BIAN FraudEvaluation (SD 44625, has a published semantic API).

The score is hardcoded. That is deliberate for now (doc 10 Part B): the value of this file
is that the *call site* exists, so replacing the constant with a real engine is a swap
rather than a search through a 234-line function.

BIAN's behaviour qualifiers for this SD name the files this grows into —
FraudEvaluationAssessment, Models, RuleSetsAndDecisionTrees (doc 09 §3). Use those names
when it grows; the internals are then derived rather than invented.

Reads  ctx: payment_oid, current_state (a real engine reads amount, parties, rail, history)
Writes ctx: fraud; current_state -> AUTHORISED -> APPROVED, payment_doc

The fraud block is `$set` on the persisted document at the same moment as the AUTHORISED
transition, so the score and the state that attests to it land in one write (§11).

TODO (Doina stage 4B Key Features):
  - real scoring over amount, beneficiary novelty, velocity, and device/channel signals.
  - a decision enum wider than APPROVED: REVIEW and DECLINED need a lifecycle terminal
    each, and DECLINED must stop the saga before stage 5.
  - AML risk scoring fed by the stage 3 purpose code.
  - the authorization decision recorded with actor + timestamp, since stage 5 execution
    depends on it (Doina: "this step depends on the result of Stage 4's decision").
"""

from __future__ import annotations

from contexts.payment_order_initiation.domain import lifecycle
from process.payment_context import PaymentContext


def run(ctx: PaymentContext) -> None:
    ctx.fraud = {"score": 5, "decision": "APPROVED"}

    # TODO: when the decision can be anything but APPROVED, raise here. The saga will mark
    # the payment REJECTED with this reason. Stage 5 must never run on an unapproved payment.
    #   if ctx.fraud["decision"] == "DECLINED":
    #       raise ValueError("Payment declined by fraud evaluation.")

    lifecycle.advance_ctx(
        ctx, lifecycle.AUTHORISED,
        actor="fraud-service",
        reason=f"Fraud score {ctx.fraud['score']} within threshold; sanctions clear",
        # `clearing.authorisedAt` is stage 4's to stamp. It used to be written at creation
        # alongside the other three, which made the demo's timeline a fiction.
        extra={"fraud": ctx.fraud, "clearing.authorisedAt": ctx.now},
    )
    lifecycle.advance_ctx(
        ctx, lifecycle.APPROVED,
        actor="transactions-service",
        reason="No dual-approval threshold breached",
    )
