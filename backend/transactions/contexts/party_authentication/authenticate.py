"""Stage 2 — party authentication & entitlement.

BIAN PartyAuthentication (SD 38917) + CustomerAccessEntitlement (SD 43057).

Doina stage 2 deliberately excludes fraud scoring and transaction-level risk
authorization — those are stage 4, once the payment is validated and enriched. Keep them
out of here; conflating identity with risk is the anti-pattern this split exists to avoid.

**Destiny: this context moves out.** Authentication is Party-domain, not payments. Today
it is one ownership check, so it stays until it has enough code to justify the move.

Reads  ctx: debtor_customer_id, customer_ref, debtor_account_ref
Writes ctx: nothing

TODO (Doina stage 2 Key Features):
  - channel authentication (the caller is who they claim) — today the API is open; the
    demo posture is documented in the umbrella architecture notes.
  - entitlement: is this user permitted to initiate a payment *of this size* on *this*
    account? Mandate / signatory limits live on `accounts.signatories`.
  - step-up authentication above a threshold, and the audit record of which factor passed.
"""

from __future__ import annotations

from process.payment_context import PaymentContext


def run(ctx: PaymentContext) -> None:
    # Entitlement, minimal form: the debtor account must belong to the requesting customer.
    if ctx.debtor_customer_id != ctx.customer_ref:
        raise ValueError(
            f"Debtor account {ctx.debtor_account_ref} is not owned by {ctx.customer_ref}."
        )
