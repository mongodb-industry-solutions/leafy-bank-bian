"""Stage 3 — payment validation (BIAN PaymentOrderInitiation, SD 42933).

Doina stage 3, validation half. Every rule here is a reason the bank refuses to create a
payment order at all, so each raises and nothing downstream runs.

Reads  ctx: debtor_account, creditor_account, debtor_account_ref, creditor_account_ref,
            instructed_amount, instructed_currency, payment_oid, current_state
Writes ctx: current_state -> VALIDATED, payment_doc

The amount-vs-limit check lives in `capture.py` instead — Doina's Stage 1 covers basic field
validation, and it needs no account data.

A failure here raises `ValueError`. The saga catches it and marks the payment REJECTED with
the reason (§11) — this module does no rejection bookkeeping of its own.

TODO (Doina stage 3 Key Features), in rough order of demo value:
  - domestic vs cross-border: compare creditor bank country against our own. This single
    comparison drives the diverging paths from stage 4 on; no flag on the instruction.
  - paymentType viability: `ctx.payment_type` is currently accepted and ignored — the
    payment doc hardcodes `type: "CREDIT_TRANSFER"`. Stage 3 should confirm the chosen
    type is reachable on the chosen rail rather than re-selecting it (it was set in
    stage 1). **Live gap, not just a future feature.**
  - IBAN / BIC structural validation, and beneficiary bank reachability on the rail.
  - purpose code against a PaymentPurposeCode reference table, feeding stage 4 AML
    scoring and routing priority — not a free-text field.
  - per-check outcomes recorded with actor + timestamp, instead of silent pass/throw,
    so the demo UI can show which checks ran and what each returned.
"""

from __future__ import annotations

from contexts.payment_order_initiation.domain import lifecycle
from process.payment_context import PaymentContext


def run(ctx: PaymentContext) -> None:
    if ctx.debtor_account["status"] == "CLOSED":
        raise ValueError("Debtor account is CLOSED.")

    debtor_currency = ctx.debtor_account.get("currency")
    if debtor_currency != ctx.instructed_currency:
        raise ValueError("Currency mismatch — FX is out of scope for Phase 1.")

    # An external beneficiary has no account here to inspect. Its status and currency are
    # the receiving bank's to police, and reachability on the rail is a stage-3 TODO
    # below. Only the two-sided rules are skipped; every debtor-side rule still runs.
    if not ctx.is_external_creditor:
        if ctx.creditor_account["status"] == "CLOSED":
            raise ValueError("Creditor account is CLOSED.")
        if ctx.debtor_account_ref == ctx.creditor_account_ref:
            raise ValueError("Debtor and creditor accounts must differ.")
        if ctx.creditor_account.get("currency") != debtor_currency:
            raise ValueError("Currency mismatch — FX is out of scope for Phase 1.")

    # Pre-flight funds floor. Re-checked inside the ACID transaction in stage 5, which is
    # the check that actually holds — this one gives a clean 400 instead of a rollback.
    available = ctx.debtor_account.get("balance", {}).get("available", 0)
    if available < ctx.instructed_amount:
        raise ValueError("Insufficient available balance in debtor account.")

    lifecycle.advance_ctx(
        ctx, lifecycle.VALIDATED,
        actor="transactions-service",
        reason="Structural and account validation passed",
    )
