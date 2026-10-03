"""Inbound stages 3 and 4 — originator screening, FX, and the accept/reject decision.

Two stages in one module because inbound's stage 4 is *one boolean* (her L823: there is no
execution-path or rail-selection decision, only "does Leafy Bank accept this payment?"), and
it rolls up a result this module has just computed. Splitting them would put a two-field
handoff across a file boundary for no gain.

## Stage 3 — FR-3.IN1..3 (her L699)

What is genuinely new: **sanctions/AML screening of the originator** and **inbound FX**.
What does NOT apply: routing data, clearing-member resolution, fee estimation — routing
already happened on the sender's side before the message arrived. And structural/account
validation is not repeated: stage 2 already did it, because account existence is a
precondition for even considering acceptance ("the sequence is reordered relative to
outgoing, not duplicated").

## Stage 4 — FR-4.IN1..3 (her L823)

`beneficiaryResolution.matchOutcome` + sanctions outcome -> ACCEPT or REJECT. On ACCEPT the
payment advances to stage 5 (the pacs.002); on REJECT it goes to the UTA queue rather than
straight to a terminal, because the funds are here and someone must decide how to return
them.

Reads  ctx: payment_doc, beneficiary_match, inbound_parsed, creditor_account
Writes ctx: payment_doc (correspondent.sanctionsCheck, fx, validation, acceptanceDecision)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from contexts.fraud_evaluation.domain import sanctions
from contexts.financial_gateway.domain import name_match
from contexts.payment_order_initiation.domain import checks, lifecycle
from contexts.payment_order_initiation.domain.bank_identity import OUR_BANK_COUNTRY
from process.payment_context import PaymentContext

logger = logging.getLogger(__name__)

STAGE_SCREEN = "3 screen originator"
STAGE_ACCEPT = "4 accept"

ACCEPT = "ACCEPT"
REJECT = "REJECT"

# Reason codes on the acceptance decision. ISO 20022 external status-reason codes, so the
# pacs.002/pacs.004 can quote them directly rather than translating a local vocabulary:
#   AC04 ClosedAccountNumber / AC01 IncorrectAccountNumber — the beneficiary could not be
#   resolved; AM05 Duplication; RR04 (regulatory reason) — the screening refusal.
REASON_BENEFICIARY_UNRESOLVED = "AC01"
REASON_SANCTIONS = "RR04"
REASON_ACCEPTED = None

# Simulated FX, mirroring stage 3's outbound posture (FR-3.14): a fixed table, clearly
# labelled SIMULATED, never a live rate. Same shape as the outbound `fx{}` object so the UI
# renders one component for both directions (her L708: "reuses the fx object design already
# proposed for outgoing unchanged — same shape, same simulated-not-live posture").
_FX_RATES = {
    ("EUR", "USD"): 1.0850,
    ("GBP", "USD"): 1.2700,
    ("CHF", "USD"): 1.1200,
    ("CAD", "USD"): 0.7350,
    ("USD", "EUR"): 0.9217,
    ("USD", "GBP"): 0.7874,
}
_FX_SOURCE = "SIMULATED-FX-v1"


def run(ctx: PaymentContext) -> None:
    """Stage 3 then stage 4, in that order — screening feeds the decision."""
    if ctx.direction != "INBOUND":  # pragma: no cover - the saga never routes it here
        return

    now = datetime.now(timezone.utc)
    recorded = []

    screening = _screen_originator(ctx, now, recorded)
    _classify_corridor(ctx, now, recorded)
    fx = _attach_fx(ctx, now, recorded)

    lifecycle.advance_ctx(
        ctx, lifecycle.ENRICHED,
        actor="financial-gateway",
        reason="Originator screened; inbound enrichment complete",
        extra=_screening_update(screening, fx, ctx.instructed_amount, now),
    )
    lifecycle.advance_ctx(
        ctx, lifecycle.FINAL_VALIDATED,
        actor="financial-gateway",
        reason="Inbound validation complete",
    )

    checks.append_checks(ctx.collections.payments, ctx.payment_oid, recorded)

    _decide(ctx, screening, now)


# --- stage 3 -----------------------------------------------------------------

def _screen_originator(ctx, now, recorded):
    """FR-3.IN1 — screen the party we are about to accept funds FROM.

    The mirror of outbound's beneficiary screen, and the same rules: `sanctions.screen`
    takes the party as a subject, so nothing is duplicated (only `party_label` differs, so
    the recorded prose says which side was screened).
    """
    debtor = (ctx.inbound_parsed or {}).get("debtor") or {}
    outcome = sanctions.screen(
        party_name=debtor.get("name"),
        party_country=_country_of(debtor),
        purpose_code=(ctx.inbound_parsed or {}).get("purposeCode"),
        party_label="Originator",
    )
    recorded.append(
        checks.check(
            STAGE_SCREEN, "originator_screened",
            checks.FAIL if outcome.refuses else checks.PASS,
            mode=checks.SYNC,
            detail=outcome.detail,
            actor="financial-gateway", at=now,
        )
    )
    return outcome


def _country_of(debtor: dict):
    """The originator's country — from the party itself, falling back to its bank's BIC.

    A pacs.008 `Dbtr` often carries no structured country (the address is a free-text line),
    but `DbtrAgt/FinInstnId/BICFI` always encodes one in characters 5-6 of the BIC. That is
    a real ISO 9362 rule, not a guess, and without it a screening against a restricted
    country would silently never fire on the field most messages actually populate.
    """
    country = debtor.get("bankCountry")
    if country:
        return country
    bic = debtor.get("bic") or ""
    return bic[4:6].upper() if len(bic) >= 6 else None


def _classify_corridor(ctx, now, recorded) -> None:
    """FR-3.IN3 — domestic vs cross-border, with the comparison inputs swapped.

    Outbound compares `creditor.bankCountry` against our home country; inbound compares
    `debtor.bankCountry`, because the external party is now the originator. Same logic, and
    her L713 asks for exactly that swap and nothing else.
    """
    debtor = (ctx.inbound_parsed or {}).get("debtor") or {}
    origin = _country_of(debtor)
    category = "DOMESTIC" if origin == OUR_BANK_COUNTRY else "CROSS_BORDER"
    ctx.collections.payments.update_one(
        {"_id": ctx.payment_oid},
        {"$set": {"validation.determinedCategory": category, "updatedAt": now}},
    )
    recorded.append(
        checks.check(
            STAGE_SCREEN, "corridor_classified", checks.PASS, mode=checks.SYNC,
            detail=(
                f"Inbound {category.replace('_', '-').lower()} payment: originator bank "
                f"country {origin or 'unknown'} vs home country {OUR_BANK_COUNTRY}."
            ),
            actor="financial-gateway", at=now,
        )
    )


def _attach_fx(ctx, now, recorded):
    """FR-3.IN2 — convert to the BENEFICIARY account's currency.

    The mirror of outbound, where the conversion is into the payer's currency. Her L708: "no
    new field is required; only the direction of interpretation differs, and that's already
    carried by the parent payment's `direction` field."

    Returns None when no conversion is needed, which is the common case.
    """
    account = ctx.creditor_account or {}
    target = account.get("currency")
    source = ctx.instructed_currency
    if not target or target == source:
        return None

    rate = _FX_RATES.get((source, target))
    if rate is None:
        # No rate for this pair. The payment is NOT refused — it is credited in the
        # instructed currency and flagged, because refusing a customer's incoming money over
        # a missing demo rate would be the wrong failure. Same posture as stage 3's thin
        # bank directory: a miss is a WARN.
        recorded.append(
            checks.check(
                STAGE_SCREEN, "inbound_fx_unavailable", checks.WARN, mode=checks.SYNC,
                detail=(
                    f"No simulated rate for {source}->{target}; crediting in {source} "
                    f"without conversion."
                ),
                actor="financial-gateway", at=now,
            )
        )
        return None

    converted = round(ctx.instructed_amount * rate, 2)
    recorded.append(
        checks.check(
            STAGE_SCREEN, "inbound_fx_applied", checks.PASS, mode=checks.SYNC,
            detail=(
                f"{source} {ctx.instructed_amount:,.2f} -> {target} {converted:,.2f} "
                f"at {rate} ({_FX_SOURCE}, SIMULATED)."
            ),
            actor="financial-gateway", at=now,
        )
    )
    # The credited amount becomes the payment's `amount`; `instructedAmount` keeps what the
    # sender said. That split already exists for outbound and is what makes the conversion
    # auditable rather than destructive.
    #
    # BOTH the amount and the currency move on the context. The payment document already
    # recorded the instructed pair at build, so mutating here does not rewrite history —
    # but stages 6-7 build the `transactions` doc and the settlement position off the
    # context, and a converted amount with the instructed currency is a wrong fact: the
    # customer was credited converted USD, not EUR. (Found by the FX scenario test, which
    # asserted the txn's currency — the amount was already right.)
    ctx.instructed_amount = converted
    ctx.instructed_currency = target
    return {
        "sourceCurrency": source,
        "targetCurrency": target,
        "fxRate": rate,
        "rateTimestamp": now,
        "rateSource": _FX_SOURCE,
        "quoteId": None,
        "simulated": True,
    }


def _screening_update(screening, fx, converted_amount, now) -> dict:
    """The stage-3 fields, written in the same `$set` as the ENRICHED transition.

    One write, so the screening outcome and the state it justifies can never disagree —
    the `extra=` mechanism `lifecycle.advance` exists for.
    """
    update = {
        "correspondent.sanctionsCheck": {
            "status": screening.status,
            "checkedAt": now,
            "provider": sanctions.PROVIDER,
        },
    }
    if fx:
        update["fx"] = fx
        update["fxRate"] = fx["fxRate"]
        # `amount` becomes the CREDITED amount; `instructedAmount` keeps what the sender
        # said. Stage 6 posts `amount`, which is what the customer actually receives.
        update["amount"] = converted_amount
        update["currency"] = fx["targetCurrency"]
    return update


# --- stage 4 -----------------------------------------------------------------

def _decide(ctx, screening, now) -> None:
    """FR-4.IN1 — the binary accept/reject, rolled up from stages 2 and 3.

    Her L829 gives the rule and her L831 the one case that matters: a sanctions hit
    *"despite a matched beneficiary"* still rejects. So both inputs must be ACCEPT-able,
    not either.
    """
    from contexts.financial_gateway.application import uta

    beneficiary_ok = ctx.beneficiary_match in name_match.PROCEEDING_OUTCOMES
    screening_ok = not screening.refuses

    if beneficiary_ok and screening_ok:
        decision, reason_code = ACCEPT, REASON_ACCEPTED
    elif not screening_ok:
        decision, reason_code = REJECT, REASON_SANCTIONS
    else:
        decision, reason_code = REJECT, REASON_BENEFICIARY_UNRESOLVED

    ctx.collections.payments.update_one(
        {"_id": ctx.payment_oid},
        {"$set": {
            "acceptanceDecision": {
                "decision": decision,
                "reasonCode": reason_code,
                "decidedAt": now,
                # The two inputs, stored alongside the roll-up so the demo panel (her L835)
                # can print the decision WITH its reasons from one document.
                "beneficiaryMatch": ctx.beneficiary_match,
                "sanctionsStatus": screening.status,
            },
            "updatedAt": now,
        }},
    )
    checks.append_checks(ctx.collections.payments, ctx.payment_oid, [
        checks.check(
            STAGE_ACCEPT, "acceptance_decided",
            checks.PASS if decision == ACCEPT else checks.FAIL,
            mode=checks.SYNC,
            detail=(
                f"Beneficiary match: {ctx.beneficiary_match}; sanctions/AML screening: "
                f"{screening.status}. Decision: {decision}"
                + (f" ({reason_code})." if reason_code else ".")
            ),
            actor="financial-gateway", at=now,
        )
    ])

    if decision == REJECT:
        # FR-4.IN3 — do not advance to stage 5/6; route to stage 9 for return processing.
        # As at stage 2, this is a HOLD and not a rejection: the money is here, and an
        # operator has to choose Repair or Return.
        uta.record(
            ctx,
            reason=(
                f"Acceptance refused ({reason_code}): beneficiary match "
                f"{ctx.beneficiary_match}, screening {screening.status}."
            ),
            stage=STAGE_ACCEPT,
        )
        ctx.halt = True
        ctx.result = ctx.collections.payments.find_one({"_id": ctx.payment_oid})
