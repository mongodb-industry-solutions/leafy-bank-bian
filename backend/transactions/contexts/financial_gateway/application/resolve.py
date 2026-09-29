"""Inbound stage 2 — message authentication + beneficiary resolution (FR-2.IN1..4).

BIAN FinancialGateway (message auth), CurrentAccount (beneficiary lookup). The inbound
counterpart of `party_authentication/authenticate.py`, and her L488 says why it is a
different question entirely: not *"is this customer allowed to send this amount?"* but
*"is this a legitimate message, and does the account it names actually belong to the person
it claims to belong to?"*

**There is no customer to authenticate.** Her L490: party authentication here means
authenticating the sending *institution* at the network level — not a login, not MFA. So
this stage never raises `StepUpRequired` and never consults an entitlement policy; both
belong to a customer session that does not exist on this path.

## Explicitly NOT here

Sanctions/AML screening of the originator. Her L507 excludes it from this stage by name and
places it at stage 3 (FR-3.IN1) — the sequence is *reordered* relative to outbound, not
duplicated. Resist the pull to screen here just because the debtor is already parsed.

Reads  ctx: payment_doc, claimed_creditor, collections
Writes ctx: beneficiary_match, creditor_account, creditor_customer, creditor_customer_id,
            creditor_account_ref, payment_doc (beneficiaryResolution + customerId)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from contexts.financial_gateway.domain import name_match
from contexts.payment_order_initiation.domain import checks, lifecycle
from process.payment_context import PaymentContext

logger = logging.getLogger(__name__)

STAGE = "2 resolve beneficiary"

# An account must be open to receive funds. `accounts.status` uses ACTIVE for an open
# account; anything else (CLOSED, FROZEN, DORMANT) cannot be credited without a human.
_OPEN_STATUS = "ACTIVE"


def run(ctx: PaymentContext) -> None:
    if ctx.direction != "INBOUND":  # pragma: no cover - the saga never routes it here
        return

    now = datetime.now(timezone.utc)
    c = ctx.collections
    claimed = ctx.claimed_creditor or {}
    recorded = []

    # --- FR-2.IN1: message / sending-institution authentication --------------
    # Consumes the outcome of the bank's existing network-integrity controls, exactly as
    # outbound stage 2 consumes the channel's authentication assertion (her L492: "the same
    # posture outgoing takes ... confirmed here at the institution/message level"). The
    # simulated gateway is a trusted channel, so this records the fact and does not gate.
    debtor_bank = (ctx.inbound_parsed or {}).get("debtor") or {}
    recorded.append(
        checks.check(
            STAGE, "inbound_message_authenticated", checks.PASS,
            mode=checks.SYNC,
            detail=(
                f"Message accepted from {debtor_bank.get('bankName') or 'sending bank'} "
                f"({debtor_bank.get('bic') or 'no BIC'}) over the simulated financial "
                f"gateway — network-level integrity assumed by the channel."
            ),
            actor="financial-gateway", at=now,
        )
    )

    # --- FR-2.IN2: does the claimed account exist, and is it open? -----------
    account = _find_claimed_account(c, claimed)
    if account is None:
        _record(ctx, recorded)
        _fail(
            ctx, name_match.NO_MATCH, name_match.METHOD_NONE, None, now,
            reason=(
                f"No account matches the claimed beneficiary "
                f"{claimed.get('accountNo') or claimed.get('iban') or '(none supplied)'}."
            ),
        )
        return

    if account.get("status") != _OPEN_STATUS:
        _record(ctx, recorded)
        _fail(
            ctx, name_match.NO_MATCH, name_match.METHOD_NONE, account.get("accountId"), now,
            reason=(
                f"Beneficiary account {account.get('accountId')} is "
                f"{account.get('status')}, not {_OPEN_STATUS} — cannot be credited."
            ),
        )
        return

    # --- FR-2.IN3: does the name on record match the one claimed? ------------
    customer_id = (account.get("customerSnapshot") or {}).get("customerId")
    customer = c.customers.find_one({"customerId": customer_id}) if customer_id else None
    name_of_record = ((customer or {}).get("identification") or {}).get("legalName")

    outcome, method = name_match.compare(claimed.get("name"), name_of_record)

    if outcome == name_match.NO_MATCH:
        _record(ctx, recorded)
        _fail(
            ctx, outcome, method, account.get("accountId"), now,
            reason=(
                f"Beneficiary name {claimed.get('name')!r} does not match the account "
                f"holder of record {name_of_record!r}."
            ),
        )
        return

    # --- MATCHED or PARTIAL: the payment proceeds ----------------------------
    _attach_confirmed_beneficiary(ctx, account, customer, customer_id)

    recorded.append(
        checks.check(
            STAGE, "beneficiary_resolved",
            checks.PASS if outcome == name_match.MATCHED else checks.WARN,
            mode=checks.SYNC,
            detail=(
                f"Claimed beneficiary {claimed.get('name')!r} resolved to account "
                f"{account.get('accountId')} ({name_of_record!r}) — {outcome} via {method}."
                + (
                    " Proceeding with a flag: the name is a plausible variant, not an "
                    "exact match." if outcome == name_match.PARTIAL else ""
                )
            ),
            actor="financial-gateway", at=now,
        )
    )
    _record(ctx, recorded)

    ctx.beneficiary_match = outcome
    _write_resolution(ctx, outcome, method, account.get("accountId"), now)

    lifecycle.advance_ctx(
        ctx, lifecycle.VALIDATED,
        actor="financial-gateway",
        reason=f"Beneficiary resolved ({outcome}); message authenticated",
        extra={"customerId": customer_id},
    )


def _find_claimed_account(c, claimed: dict):
    """Look the claimed beneficiary up by IBAN or account number.

    Both are external identifiers the sender supplied, so neither is our `accountId` — the
    lookup is by `iban` / `accountNumber`, the fields a counterparty can actually know.
    """
    iban = claimed.get("iban")
    if iban:
        found = c.accounts.find_one({"iban": iban})
        if found is not None:
            return found
    account_no = claimed.get("accountNo")
    if account_no:
        return c.accounts.find_one({"accountNumber": account_no})
    return None


def _attach_confirmed_beneficiary(ctx, account, customer, customer_id) -> None:
    """Promote the claimed beneficiary to a confirmed one on the context.

    Until this runs, `creditor_account` is None and `payments.creditor.accountId` is null —
    the claimed/confirmed distinction (her L387). Stages 6 and 7 credit
    `ctx.creditor_account_ref`, so this is the moment an inbound payment acquires a real
    destination.
    """
    ctx.creditor_account = account
    ctx.creditor_account_ref = account.get("accountId")
    ctx.creditor_customer = customer
    ctx.creditor_customer_id = customer_id


def _write_resolution(ctx, outcome, method, matched_account_id, now) -> None:
    """DR-2.IN1 — the `beneficiaryResolution` object, and the confirmed creditor fields.

    Written as one `$set` so the outcome and the account it resolved to can never disagree.
    `creditor.accountId` moves from null (claimed) to the resolved id here and nowhere else.
    """
    update = {
        "beneficiaryResolution": {
            "matchOutcome": outcome,
            "matchedAccountId": matched_account_id,
            "matchMethod": method,
            "checkedAt": now,
        },
        "updatedAt": now,
    }
    if matched_account_id and outcome in name_match.PROCEEDING_OUTCOMES:
        account = ctx.creditor_account or {}
        customer = ctx.creditor_customer or {}
        update["creditor.accountId"] = matched_account_id
        update["creditor.accountNo"] = account.get("accountNumber")
        # The name of RECORD replaces the claimed one only when the match was exact or
        # normalised. On a PARTIAL the claimed name is kept: overwriting it would erase the
        # very discrepancy the flag exists to report, and an operator reviewing the queue
        # needs to see what the sender actually said.
        if outcome == name_match.MATCHED:
            update["creditor.name"] = (
                (customer.get("identification") or {}).get("legalName")
            )
    ctx.collections.payments.update_one({"_id": ctx.payment_oid}, {"$set": update})


def _record(ctx, recorded) -> None:
    if recorded:
        checks.append_checks(ctx.collections.payments, ctx.payment_oid, recorded)


def _fail(ctx, outcome, method, matched_account_id, now, *, reason: str) -> None:
    """FR-2.IN4 — a NO_MATCH does not reject the payment; it routes to Unable to Apply.

    ⚠️ **Not a `ValueError`.** The saga's rejection path would mark the payment REJECTED and
    close it, but the money has already arrived — and her L1331 is explicit that an inbound
    payment that cannot be credited goes to the **UTA queue** where an operator chooses
    Repair or Return (FR-9.IN2). Rejecting it here would strand the funds with no operator
    path and nothing to return them with.

    So the resolution is recorded, an open UTA exception is raised into the queue, and the
    saga halts with the payment parked. `resolve_uta` resumes or returns it.
    """
    from contexts.financial_gateway.application import uta

    ctx.beneficiary_match = outcome
    _write_resolution(ctx, outcome, method, matched_account_id, now)

    uta.record(ctx, reason=reason, stage=STAGE)

    logger.info("inbound payment %s held as Unable to Apply: %s", ctx.payment_id, reason)
    ctx.halt = True
    ctx.result = ctx.collections.payments.find_one({"_id": ctx.payment_oid})
