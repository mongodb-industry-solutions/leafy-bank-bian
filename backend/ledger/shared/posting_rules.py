"""Posting rules — pure, table-driven decomposition of a payment into balanced ledger legs.

Zero I/O: takes the in-memory ``ChartOfAccounts`` as input so the ``ingest_worker`` can
decompose each event without a per-event DB read (latency hazard). Returns a *list* of
``PostingLeg`` — 2 for a principal-only payment today, N once fees/tax/FX add legs. Adding a
leg type is a new rule here, never a refactor of the callers (resolved #3/#4, Phase-1 scope).

``MAPPING_VERSION`` is stamped on each emitted event as a provenance/version marker.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from .coa_cache import ChartOfAccounts

# Bump on any rule change. Stamped on each emitted event as a provenance/version marker.
MAPPING_VERSION = "1.0.0"

# eventType — subset of the spec's ledgerEvents.eventType enum exercised in Phase 1.
EVENT_PAYMENT_PRINCIPAL = "PAYMENT_PRINCIPAL"

# Accounting sides.
SIDE_DEBIT = "DEBIT"
SIDE_CREDIT = "CREDIT"

# Bank-side GL accounts keyed on eventType, for legs that do NOT correspond to a customer
# account (e.g. fee income). Principal legs post to the customer account's OWN control
# account (read from account.gl.accountCode), so no account-type table is needed here and
# account-type enum drift cannot bite. Extend this map when the fee/tax/FX rules land.
BANK_GL_ACCOUNT_BY_EVENT_TYPE: dict[str, str] = {
    # "FEE": "4200",   # Fee Income — added with the fee rule (future)
}


@dataclass(frozen=True)
class PostingLeg:
    """One accounting fact: one side of one component, against one GL posting account."""

    event_type: str
    side: str               # SIDE_DEBIT | SIDE_CREDIT
    gl_account_code: str     # a posting leaf (isPostingAccount: true)
    amount_minor: int        # long minor units (banker's-rounded)
    currency: str
    account_id: str          # operational account this leg relates to (entity reference)


def to_minor_units(amount: float | str | Decimal, exponent: int = 2) -> int:
    """Convert a major-unit money amount to integer minor units, banker's rounding.

    The ledger stores money as long minor units; ``transactions.amount`` is a ``double``
    (money-in-float anti-pattern, resolved #9). This is the explicit, rounding-safe
    conversion at the decomposition boundary. ``Decimal(str(x))`` avoids binary-float error;
    ROUND_HALF_EVEN is banker's rounding. Phase 1 is USD-only (exponent 2) — multi-currency
    minor-unit exponents are future work.
    """
    scaled = Decimal(str(amount)).scaleb(exponent).to_integral_value(rounding=ROUND_HALF_EVEN)
    return int(scaled)


def _principal_gl_account(account: dict, coa: ChartOfAccounts) -> str:
    """The GL control account a customer account's principal leg posts to.

    The account carries its own control account in ``gl.accountCode`` (the account is
    authoritative for its own GL assignment); we trust it and only validate that it is a
    real postable leaf. Avoids duplicating an account-type -> control-code table and the
    enum drift such a table would invite.
    """
    code = (account.get("gl") or {}).get("accountCode")
    if not code:
        raise ValueError(f"account {account.get('accountId')!r} has no gl.accountCode")
    coa.require_active_posting_account(code)  # raises if missing / not posting / not ACTIVE
    return code


def decompose_principal_payment(
    *,
    amount: float | str | Decimal,
    currency: str,
    debtor_account: dict,
    creditor_account: dict,
    coa: ChartOfAccounts,
) -> list[PostingLeg]:
    """Decompose a principal-only payment into a balanced DEBIT/CREDIT leg pair.

    A customer deposit is a bank liability (normal balance CREDIT):
      - debtor (money out)  -> DEBIT  its deposit control account (liability falls)
      - creditor (money in) -> CREDIT its deposit control account (liability rises)

    For an internal transfer both legs hit Customer Deposits, netting to zero on the
    control account — the bank's total deposit liability is unchanged (deposit->deposit
    model). No FX: debtor and creditor amounts are equal, so the pair balances by
    construction.
    """
    amount_minor = to_minor_units(amount)
    if amount_minor <= 0:
        raise ValueError(f"payment amount must be positive, got {amount!r}")

    legs = [
        PostingLeg(
            event_type=EVENT_PAYMENT_PRINCIPAL,
            side=SIDE_DEBIT,
            gl_account_code=_principal_gl_account(debtor_account, coa),
            amount_minor=amount_minor,
            currency=currency,
            account_id=debtor_account["accountId"],
        ),
        PostingLeg(
            event_type=EVENT_PAYMENT_PRINCIPAL,
            side=SIDE_CREDIT,
            gl_account_code=_principal_gl_account(creditor_account, coa),
            amount_minor=amount_minor,
            currency=currency,
            account_id=creditor_account["accountId"],
        ),
    ]
    assert_balanced(legs)
    return legs


def assert_balanced(legs: list[PostingLeg]) -> None:
    """Guard: ΣDEBIT minor units == ΣCREDIT minor units across the legs.

    Trivially true for a principal pair, but this is the invariant that protects the
    journal builder when fees/tax multiply the legs. The summation is deliberately general
    (sum whatever legs are present) so it keeps holding as legs grow.
    """
    debit = sum(leg.amount_minor for leg in legs if leg.side == SIDE_DEBIT)
    credit = sum(leg.amount_minor for leg in legs if leg.side == SIDE_CREDIT)
    if debit != credit:
        raise ValueError(f"unbalanced legs: sum(DEBIT)={debit} != sum(CREDIT)={credit}")