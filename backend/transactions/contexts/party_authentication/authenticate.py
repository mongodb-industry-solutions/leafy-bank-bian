"""Stage 2 — party authentication & entitlement.

BIAN PartyAuthentication (SD 38917, control record `PartyAuthenticationAssessment`) +
CustomerAccessEntitlement (SD 43057, control record `CustomerAccessProfileAgreement`,
behaviour qualifier `Restrictions/Evaluate`).

## This stage does not authenticate anybody

There is no authentication in this demo to perform: `customerId` is a plain body field, CORS
is open, and the frontend persona is a `localStorage` value. Building real auth is a
demo-wide programme across three services and the UI, and Doina's scope section puts
production-grade security controls out of scope.

So the payments hub **consumes an authentication assertion from the channel** and verifies
it is present, fresh enough to name, and strong enough for the amount. That is what BIAN
models: `PartyAuthentication` is a *Cross Channel* service domain, and its control record is
"the result of an authentication someone else performed" (doc 15 B1).

**When no assertion is supplied the check is SKIP, never PASS.** Today an absent
authentication is indistinguishable from a successful one; after this it is recorded as
`method: NONE` and rendered as a non-pass. A real identity provider drops into this seam
without changing the stage.

## Stage 2 adds no lifecycle state

Doina's canonical sequence goes `INITIATED -> VALIDATED` with nothing between, and the spec
enum and `_FORWARD` agree. This is a **gate**: it passes, or it raises `ValueError` and the
saga marks the payment REJECTED at INITIATED. Its visibility comes from `checks[]`, not from
the state machine (doc 15 B5).

Reads  ctx: debtor_customer_id, customer_ref, debtor_account_ref, debtor_account,
            debtor_customer, instructed_amount, authentication, payment_oid, collections
Writes ctx: nothing on the context; six `payments.checks[]` entries, one `authentication{}`
            assessment block, and `entitlement.dualApprovalRequired` on the document

Deliberately NOT here: fraud scoring and transaction-level risk authorization. Doina moves
both to stage 4b ("that moves to Stage 4") — conflating identity with risk is the
anti-pattern this split exists to avoid.

Deferred with a home (doc 15 §6): the **interactive** dual-approval queue. A genuinely
obtained second approval needs a pause, an approve endpoint and a Payments Operations
screen, and that persona has no UI yet. Phase 1 discharges the requirement with a
**labelled simulated approver** so the flagship $25,000 payment still settles end to end
(doc 15 B4, Doina Q15).
"""

from __future__ import annotations

from datetime import datetime, timezone

from contexts.party_authentication.domain import entitlement_policy as policy
from contexts.payment_order_initiation.domain import checks
from process.payment_context import PaymentContext

STAGE = "2 authenticate"

# Account states that forbid a debit. `DORMANT` and `FROZEN` are in the canonical
# `CurrentAccountApexStatus` enum and were never checked anywhere — a frozen account could
# be debited. `CLOSED` moved here from `validation.py` (stage 3): entitlement to *use the
# account* is stage 2's question, validity of the *payment* is stage 3's (doc 15 B6). The
# creditor-side `CLOSED` check stays in stage 3, where it belongs.
_UNUSABLE_ACCOUNT_STATES = frozenset({"CLOSED", "DORMANT", "FROZEN"})

# The approver a simulated dual approval is recorded against. A real second approver
# arrives with the approval queue; until then the name in the audit trail must make the
# simulation obvious to anyone reading it, including on a demo screen.
SIMULATED_APPROVER = "SIMULATED-APPROVER-OPS"


def run(ctx: PaymentContext) -> None:
    now = datetime.now(timezone.utc)
    recorded: list = []

    def record(name: str, result: str, detail: str, *, actor: str = "transactions-service") -> None:
        recorded.append(
            checks.check(STAGE, name, result, mode=checks.SYNC, detail=detail, actor=actor, at=now)
        )

    def refuse(name: str, detail: str) -> None:
        """Record the failing check, flush the trail, and raise.

        The flush has to happen before the raise: the saga catches `ValueError` and marks
        the payment REJECTED, so a check written after that point would never exist. What
        the demo needs from a refusal is *which* check refused it.
        """
        record(name, checks.FAIL, detail)
        _flush(ctx, recorded)
        raise ValueError(detail)

    account = ctx.debtor_account
    customer = ctx.debtor_customer or {}
    segment = customer.get("segment")
    amount = ctx.instructed_amount

    # --- 1. customer_authenticated (R1/R3) -----------------------------------
    assertion = ctx.authentication or {}
    method = assertion.get("method") or "NONE"
    factor_count = assertion.get("factorCount") or 0
    if not ctx.authentication or method == "NONE":
        record(
            "customer_authenticated", checks.SKIP,
            "No channel authentication assertion supplied; recorded as NONE. "
            "The payments hub performs no authentication of its own.",
        )
    elif not policy.authentication_sufficient(method, factor_count, amount, segment):
        refuse(
            "customer_authenticated",
            f"Authentication method {method} with {factor_count} factor(s) is not "
            f"sufficient for {amount:,.2f} — step-up authentication required.",
        )
    else:
        record(
            "customer_authenticated", checks.PASS,
            f"Channel asserted {method} ({factor_count} factor(s))"
            + (f", session {assertion.get('sessionRef')}" if assertion.get("sessionRef") else ""),
        )

    # --- 2. account_active (R4) ---------------------------------------------
    status = account.get("status")
    if status in _UNUSABLE_ACCOUNT_STATES:
        refuse("account_active", f"Debtor account is {status}.")
    record("account_active", checks.PASS, f"Debtor account status {status}.")

    # --- 3. account_unrestricted (R6) ---------------------------------------
    blocking = policy.blocking_restrictions(account.get("restrictions"), now=now, side="DEBIT")
    if blocking:
        types = ", ".join(sorted({r["type"] for r in blocking}))
        reasons = "; ".join(r.get("reason") or "no reason recorded" for r in blocking)
        refuse("account_unrestricted", f"Debtor account carries an active {types} ({reasons}).")
    record("account_unrestricted", checks.PASS, "No active debit restriction on the debtor account.")

    # --- 4. customer_entitled (R2/R5) ---------------------------------------
    # Ownership first — the assertion that was this whole stage before today. It detects a
    # mismatched claim; it authenticates nothing, which is why check 1 exists.
    if ctx.debtor_customer_id != ctx.customer_ref:
        refuse(
            "customer_entitled",
            f"Debtor account {ctx.debtor_account_ref} is not owned by {ctx.customer_ref}.",
        )
    signatory = policy.signatory_for(account.get("signatories"), ctx.customer_ref)
    if signatory is None:
        refuse(
            "customer_entitled",
            f"{ctx.customer_ref} is not a signatory on account {ctx.debtor_account_ref}.",
        )
    if customer.get("status") != "ACTIVE":
        refuse("customer_entitled", f"Customer {ctx.customer_ref} is {customer.get('status')}.")
    kyc_status = (customer.get("kyc") or {}).get("status")
    if kyc_status != "VERIFIED":
        refuse("customer_entitled", f"Customer {ctx.customer_ref} KYC status is {kyc_status}.")
    signing_rule = signatory.get("signingRule")
    record(
        "customer_entitled", checks.PASS,
        f"{ctx.customer_ref} is a {signatory.get('type')} signatory "
        f"({signing_rule}); customer ACTIVE, KYC VERIFIED.",
    )

    # --- 5. payment_limit_available (R7) ------------------------------------
    limits = policy.policy_for(segment)
    if not policy.limit_available(amount, segment):
        refuse(
            "payment_limit_available",
            f"{amount:,.2f} exceeds the {segment or 'default'} per-payment entitlement "
            f"of {limits['perPaymentLimit']:,.2f}.",
        )
    record(
        "payment_limit_available", checks.PASS,
        f"{amount:,.2f} within the {segment or 'default'} per-payment entitlement "
        f"of {limits['perPaymentLimit']:,.2f}.",
    )

    # --- 6. dual_approval (R8/R9) -------------------------------------------
    # Stage 2 DECIDES the requirement; stage 4b keeps the APPROVED transition it already
    # owns and now reads this decision instead of asserting one (doc 15 B4).
    approval_required = policy.approval_required(amount, segment, signing_rule)
    if approval_required:
        record(
            "dual_approval", checks.PASS,
            f"{amount:,.2f} is above the {segment or 'default'} dual-approval threshold of "
            f"{limits['dualApprovalThreshold']:,.2f} (signing rule {signing_rule}); "
            f"second approver {SIMULATED_APPROVER} — SIMULATED, no human approved this "
            f"payment. The interactive approval queue is deferred to stage 2b.",
            actor=SIMULATED_APPROVER,
        )
    else:
        record(
            "dual_approval", checks.SKIP,
            f"{amount:,.2f} is at or below the {segment or 'default'} dual-approval "
            f"threshold of {limits['dualApprovalThreshold']:,.2f} and the mandate is "
            f"{signing_rule} — no second approver required.",
        )

    _flush(
        ctx, recorded,
        extra={
            # `PartyAuthenticationAssessment`-shaped: what the channel asserted and what we
            # made of it. `method: NONE` when nothing was asserted, so the record is
            # explicit about the absence rather than silent about it.
            "authentication": {
                "method": method,
                "authenticatedAt": assertion.get("authenticatedAt"),
                "sessionRef": assertion.get("sessionRef"),
                "factorCount": factor_count,
                # CUSTOMER / OPERATOR / API when the assertion came from a verified
                # token, absent when it came from a request body. This is the distinction
                # Doina's requirement 1 asks for — "the authenticated customer, corporate
                # user, or API" — and only a token can answer it honestly.
                "callerType": assertion.get("callerType"),
                # True when the session was raised by PartyAuthentication Question/Evaluate
                # because this amount was over the segment's step-up threshold. The audit
                # question "why did a second factor appear on this payment?" is answerable
                # from the payment alone, without correlating logs.
                "stepUp": bool(assertion.get("stepUp")),
                "assessedAt": now,
                "assessedBy": "transactions-service",
                "sufficient": method != "NONE",
            },
            # Read by stage 4b for the APPROVED transition's reason.
            "entitlement": {
                "segment": segment,
                "signingRule": signing_rule,
                "perPaymentLimit": limits["perPaymentLimit"],
                "dualApprovalThreshold": limits["dualApprovalThreshold"],
                "dualApprovalRequired": approval_required,
                "dualApprovalBy": SIMULATED_APPROVER if approval_required else None,
                "dualApprovalSimulated": approval_required,
                "assessedAt": now,
            },
        },
    )


def _flush(ctx: PaymentContext, recorded: list, *, extra: dict | None = None) -> None:
    checks.append_checks(ctx.collections.payments, ctx.payment_oid, recorded)
    recorded.clear()
    if extra:
        ctx.collections.payments.update_one({"_id": ctx.payment_oid}, {"$set": extra})
