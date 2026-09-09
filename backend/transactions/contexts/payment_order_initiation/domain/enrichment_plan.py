"""What enrichment resolved, as data. Doc 17 §3 step 5, decision B1.

The pure half of stage 3's enrichment: it takes the payment document and a `ReferenceData`
port, and returns **a plan** — the field updates to apply, the per-field diff to record, and
the outcomes to log as checks. It writes nothing. `enrichment.py` applies it.

That split exists because of B1's real problem.

## The problem B1 solves

The payment document holds **one value per field**. `creditor.name` is "Supplier XYZ" until
enrichment makes it "Supplier XYZ Ltd.", and then what the customer typed is **gone**. Her
stage-3 centrepiece (L459-460) is a before/after comparison:

    Supplier XYZ Account 123456
      ↓ enrichment
    Supplier XYZ Ltd., 123 Business Street New York NY, Account US123456…,
    BIC ABCDUS33, Purpose: SUPP, Remittance: INV-48392

Both halves must be on screen at once, and the right-hand half destroys the left. Doc 07 §3.3:
*"There is no as-captured snapshot, so the 'before' half cannot be read back from the payment
document. **This blocks the demo screen, not just the data model.**"*

So every resolution is recorded as `{field, from, to, source}`, and the pre-enrichment values
of exactly the fields we touch are snapshotted into `original{}`. `original` is taken from the
**persisted document**, not the request — which is what makes it provably as-captured, with no
plumbing back to the API layer.

## Nothing here refuses

Enrichment is best-effort (doc 17 B7). A directory miss is a WARN, never a `ValueError`: one
absent seed row must not fail every wire in the demo. Refusals belong to `validation.run`
(the caller's error, before enrichment) and `final_validation` (what enrichment failed to
supply, after it).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from contexts.payment_order_initiation.domain import bank_identity

# A flat wire fee, in the payment's own currency. Phase 1 has no fee schedule and no pricing
# service; this is a demo constant so `fees[]` stops being permanently empty (R18). It moves
# no money — see doc 17 §7 watch item 1: `amount` is the ledger's input and must not change.
WIRE_FEE = 25.00
# ⚠️ Sourced from the spec's `fees.items.properties.type.enum`
# (`WIRE_FEE | FX_MARKUP | INTERMEDIARY | RAIL_FEE`), not from intuition. This was
# `"WIRE_TRANSFER_FEE"` — an out-of-enum value shipped by stage 3 and written onto every
# external wire, because the drift test never recursed into `items.properties` (defect
# 2026-09-01 `guard-gap`). Fixed here; the guard was widened in the same change.
WIRE_FEE_TYPE = "WIRE_FEE"

# Rails that address institutions through a clearing system, and therefore need a member id
# on both agents. INTERNAL is deliberately absent: a book transfer between two accounts we
# hold never reaches a clearing system, and the spec agrees — its own `internal_transfer`
# sample has `clearingSystemMemberId` and `clearingSystemCode` NULL on both sides. Stamping
# USABA routing numbers on an intrabank transfer would be inventing interbank addressing for
# a payment that has none.
INTERBANK_RAILS = frozenset({"WIRE", "ACH", "RTP"})

# `fees[].chargedTo` follows ISO 20022 `chargeBearer`: who actually pays.
_FEE_PAYER_BY_CHARGE_BEARER = {
    "DEBT": "DEBTOR",     # all charges borne by the debtor
    "CRED": "CREDITOR",   # all charges borne by the creditor
    "SHAR": "SHARED",     # shared between them
    "SLEV": "DEBTOR",     # following service level — the debtor's side, for our purposes
}

# Simulated FX rates for the demo (FR-3.14). Phase 1 has no live FX provider — a
# mock/fixed table keyed by (instructed_currency, debtor_account_currency). `amount` is
# diverged to `instructedAmount * rate` in the debtor account's currency (the settlement
# currency per the spec). A USD-to-USD international wire hits the no-op path below.
SIMULATED_FX_RATES = {
    ("EUR", "USD"): 1.08,
    ("GBP", "USD"): 1.27,
    ("USD", "EUR"): 0.92,
    ("USD", "GBP"): 0.79,
}
_DEFAULT_SIMULATED_RATE = 1.0

# FR-3.12 — regulatory reporting. The canonical spec declares `correspondent.
# regulatoryReports` as a **required** array of `PaymentRegulatoryReportingRecord` ("Empty
# array if none") but gives **no item schema** — the element shape is undefined (Q24, unsent).
# The shape below is a temporary one authored to satisfy FR-3.12 ("populate when cross-border
# or above threshold") and aligned with the sibling `sanctionsCheck{status, checkedAt,
# provider}` record: a status, a timestamp, and an authority, plus the trigger reason and a
# `simulated` flag. Doina ratifies the real shape as Q24; if she renames fields it is a
# document/seed migration, not a pipeline change. `simulated: true` because the demo determines
# a report is REQUIRED but files nothing with any authority.
REGULATORY_AUTHORITY = "FINCEN-SIMULATED"
REGULATORY_REPORT_STATUS = "REQUIRED"
_REPORT_TYPE_CROSS_BORDER = "CROSS_BORDER_DECLARATION"
_REPORT_TYPE_THRESHOLD = "THRESHOLD_REPORT"
_DEFAULT_REGULATORY_REPORT_THRESHOLD_USD = 10_000.0


def regulatory_report_threshold() -> float:
    """The amount at which a wire attracts a threshold regulatory report.

    Read **lazily**, never at module scope: a module-level `os.getenv` runs before
    `load_dotenv` and silently takes the default (defects.md 2026-06-30). Same discipline as
    `duplicate_detection.window_seconds`. Default 10,000 — the US BSA Currency Transaction
    Report threshold, recognizable to an FSI audience. Env-configurable so a demo scenario can
    move it without a code change.
    """
    raw = os.getenv("REGULATORY_REPORT_THRESHOLD_USD")
    if raw is None:
        return _DEFAULT_REGULATORY_REPORT_THRESHOLD_USD
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_REGULATORY_REPORT_THRESHOLD_USD
    return value if value > 0 else _DEFAULT_REGULATORY_REPORT_THRESHOLD_USD


@dataclass
class EnrichmentPlan:
    """The outcome of planning. `updates` are dotted paths for a single `$set`."""

    updates: dict[str, Any] = field(default_factory=dict)
    resolved: list[dict] = field(default_factory=list)
    # (check_name, result, detail) — applied by the caller through `checks.check`.
    outcomes: list[tuple[str, str, str]] = field(default_factory=list)

    def _set(self, path: str, value: Any, *, before: Any, source: str) -> None:
        """Record a resolution, but only when it actually changes something.

        A no-op resolution must not appear in the diff: the before/after panel would then
        show rows where nothing happened, which is exactly the noise that makes a diff
        unreadable.
        """
        if before == value or value is None:
            return
        self.updates[path] = value
        self.resolved.append(
            {"field": path, "from": before, "to": value, "source": source}
        )

    @property
    def changed(self) -> bool:
        return bool(self.updates)


def plan(
    payment: dict, reference_data, *, external_creditor: bool,
    debtor_account_currency: Optional[str] = None,
) -> EnrichmentPlan:
    """Everything stage 3 can resolve for this payment, without writing anything.

    `payment` is the persisted document — the same object `original{}` is snapshotted from,
    so a `from` value in the diff is always what was really stored.
    """
    p = EnrichmentPlan()
    rail = payment.get("rail")

    _plan_our_side(p, payment, rail)
    _plan_creditor_bank(
        p, payment, reference_data, rail, external_creditor=external_creditor
    )
    _plan_purpose_codes(p, payment, reference_data)
    _plan_fees(p, payment, rail)
    _plan_initiating_party(p, payment)
    _plan_fx(p, payment, debtor_account_currency)
    _plan_regulatory_reports(p, payment)
    return p


# --------------------------------------------------------------------------- #

def _plan_our_side(p: EnrichmentPlan, payment: dict, rail: Optional[str]) -> None:
    """R14 — our own clearing member id, on whichever side we hold.

    The debtor is always us for an outbound payment, and `party_snapshot` writes
    `clearingSystemMemberId: None` at initiation. So on a domestic wire the ORIGINATING bank
    had no routing number — the precise gap D10 named when it said the spec's own
    `wire_domestic` sample is unsendable without it.

    Sourced from the `bank_identity` constant rather than the directory: our own BIC must not
    make the money path depend on a lookup (doc 17 B6). The two are asserted equal in tests.
    """
    if rail not in INTERBANK_RAILS:
        return
    debtor = payment.get("debtor") or {}
    if debtor.get("bic") != bank_identity.OUR_BIC:
        # Not our account on the debtor side — nothing here is ours to state.
        return
    p._set(
        "debtor.clearingSystemMemberId", bank_identity.OUR_ABA,
        before=debtor.get("clearingSystemMemberId"), source="bank_identity",
    )
    p._set(
        "debtor.clearingSystemCode", bank_identity.OUR_CLEARING_SYSTEM_CODE,
        before=debtor.get("clearingSystemCode"), source="bank_identity",
    )


def _plan_creditor_bank(
    p: EnrichmentPlan, payment: dict, reference_data, rail: Optional[str], *,
    external_creditor: bool
) -> None:
    """R7/R13/R14 — resolve the beneficiary bank from the directory.

    For an internal creditor the bank is us, and `party_snapshot` already stamped our BIC and
    name; only the clearing member id is missing. For an external one, everything the caller
    did not supply is resolvable — and a miss is a WARN.
    """
    creditor = payment.get("creditor") or {}

    if not external_creditor:
        if rail in INTERBANK_RAILS:
            p._set(
                "creditor.clearingSystemMemberId", bank_identity.OUR_ABA,
                before=creditor.get("clearingSystemMemberId"), source="bank_identity",
            )
            p._set(
                "creditor.clearingSystemCode", bank_identity.OUR_CLEARING_SYSTEM_CODE,
                before=creditor.get("clearingSystemCode"), source="bank_identity",
            )
        p.outcomes.append((
            "beneficiary_bank_resolved", "PASS",
            "Beneficiary is held by this bank; agent identity is our own."
            + ("" if rail in INTERBANK_RAILS else
               f" Rail {rail} needs no clearing-system addressing."),
        ))
        return

    bic = creditor.get("bic")
    record = reference_data.bank_by_bic(bic) if bic else None

    if record is None and creditor.get("clearingSystemMemberId"):
        # The caller may have given a routing number instead of a BIC.
        record = reference_data.bank_by_clearing_member(
            creditor.get("clearingSystemCode"), creditor.get("clearingSystemMemberId")
        )

    if record is None:
        p.outcomes.append((
            "beneficiary_bank_resolved", "WARN",
            f"Beneficiary bank {bic or 'unidentified'} is not in the institution directory. "
            "The payment carries what the caller supplied; nothing was resolved. Not "
            "refused — a thin directory is our gap, not the caller's.",
        ))
        return

    source = "correspondentBanks"
    p._set("creditor.bic", record.bic, before=creditor.get("bic"), source=source)
    p._set("creditor.bankName", record.bank_name,
           before=creditor.get("bankName"), source=source)
    p._set("creditor.bankCountry", record.bank_country,
           before=creditor.get("bankCountry"), source=source)
    p._set("creditor.clearingSystemMemberId", record.clearing_system_member_id,
           before=creditor.get("clearingSystemMemberId"), source=source)
    p._set("creditor.clearingSystemCode", record.clearing_system_code,
           before=creditor.get("clearingSystemCode"), source=source)

    corridor = (
        "DOMESTIC" if record.bank_country == bank_identity.OUR_BANK_COUNTRY
        else "INTERNATIONAL"
    )
    p.outcomes.append((
        "beneficiary_bank_resolved", "PASS",
        f"{record.bic} resolved to {record.bank_name} ({record.bank_country})"
        + (f", {record.clearing_system_code} {record.clearing_system_member_id}"
           if record.clearing_system_member_id else "")
        + f" — {corridor} corridor.",
    ))


def _plan_purpose_codes(p: EnrichmentPlan, payment: dict, reference_data) -> None:
    """R16 — validate the purpose code against the reference table.

    Her L446 wants it *"tracked via a formal PaymentPurposeCode / CategoryPurposeCode
    reference table … not just a free-text field"*. That is satisfied by checking the supplied
    code against the table; an unrecognised code is a WARN, not a refusal, and
    `remittance.purposeCode` is mirrored from `categoryPurpose` when only one was given.

    Semantic resolution of free-text remittance → purpose code is deferred (doc 17 §6, Q27).
    """
    category = payment.get("categoryPurpose")
    remittance_code = (payment.get("remittance") or {}).get("purposeCode")
    supplied = category or remittance_code

    if not supplied:
        p.outcomes.append((
            "purpose_code_resolved", "SKIP",
            "No purpose code was supplied. Phase 1 does not infer one — semantic matching "
            "against the purpose-code table is deferred.",
        ))
        return

    record = reference_data.purpose_code(supplied)
    if record is None:
        p.outcomes.append((
            "purpose_code_resolved", "WARN",
            f"Purpose code {supplied!r} is not in the reference table. Carried as supplied.",
        ))
        return

    source = "purposeCodes"
    p._set("categoryPurpose", record.code, before=category, source=source)
    p._set("remittance.purposeCode", record.code, before=remittance_code, source=source)
    p.outcomes.append((
        "purpose_code_resolved", "PASS",
        f"{record.code} — {record.name}"
        + (f" ({record.category})" if record.category else "")
        + ". Feeds AML scoring and routing priority in stage 4.",
    ))


def _plan_fees(p: EnrichmentPlan, payment: dict, rail: Optional[str]) -> None:
    """R18 — charges.

    A flat fee for a wire, nothing for an internal transfer. ⚠️ **`amount` must not change.**
    It is the settlement amount and the ledger's primary input (doc 17 §7): a fee is recorded,
    not deducted. If a later stage makes fees move money, that is a boundary change needing
    its own plan.
    """
    if rail != "WIRE":
        p.outcomes.append((
            "charges_calculated", "SKIP",
            f"No charge applies on rail {rail}.",
        ))
        return

    if payment.get("fees"):
        p.outcomes.append((
            "charges_calculated", "SKIP", "Charges were already present on the payment.",
        ))
        return

    charge_bearer = payment.get("chargeBearer")
    fee = {
        "type": WIRE_FEE_TYPE,
        "amount": WIRE_FEE,
        "currency": payment.get("currency"),
        "chargedTo": _FEE_PAYER_BY_CHARGE_BEARER.get(charge_bearer, "DEBTOR"),
    }
    p._set("fees", [fee], before=payment.get("fees"), source="fee-schedule")
    p.outcomes.append((
        "charges_calculated", "PASS",
        f"{WIRE_FEE:,.2f} {fee['currency']} wire fee, borne by {fee['chargedTo']} "
        f"(chargeBearer {charge_bearer}). Recorded only — the settlement amount is "
        "unchanged.",
    ))


def _plan_initiating_party(p: EnrichmentPlan, payment: dict) -> None:
    """R20 — FATF R.16's *Initiating Party*: who submitted the instruction.

    Her L456: *"party submitting the instruction; this is not automatically the ultimate
    debtor."* That distinction only became answerable when Level-1 authentication started
    minting a real `callerType`: an OPERATOR-initiated payment has an initiating party who is
    demonstrably not the account holder.

    `ultimateDebtor` / `ultimateCreditor` stay pass-through — neither Phase-1 scenario has a
    party distinct from the account holder (doc 17 §6).
    """
    if payment.get("rail") != "WIRE":
        return

    wire = payment.get("wireDetails") or {}
    if wire.get("initiatingParty"):
        return

    auth = payment.get("authentication") or {}
    caller_type = auth.get("callerType")
    initiated_by = (payment.get("initiation") or {}).get("initiatedBy")
    if not caller_type and not initiated_by:
        return

    party = {
        "name": initiated_by or "unknown",
        "identification": f"{caller_type or 'UNKNOWN'}:{initiated_by or 'unknown'}",
    }
    p._set("wireDetails.initiatingParty", party,
           before=wire.get("initiatingParty"), source="authentication")


def _plan_fx(p: EnrichmentPlan, payment: dict,
              debtor_account_currency: Optional[str]) -> None:
    """FR-3.14 — attach a SIMULATED FX rate when the instructed currency
    differs from the debtor account currency.

    Writes `fxRate` (spec-declared scalar, `propose_payments.json` ~642), diverges
    `amount` to the settlement amount, and sets `currency` to the debtor account's
    currency (the settlement currency per the spec: "Settlement amount after FX" /
    "Settlement currency"). Records a `fx_rate_applied` check.

    No-op when currencies match (or debtor account currency is unknown) — a
    USD-to-USD international wire is unaffected. The actual money move in
    `execute.py` uses `ctx.instructed_amount`; the diverged `amount` is the document's
    settlement-amount field for display and audit, not the debit amount. No real FX
    settlement exists in the demo, so this is honest as a labelled simulation.
    """
    instructed_currency = payment.get("instructedCurrency")
    if (not debtor_account_currency
            or debtor_account_currency == instructed_currency):
        p.outcomes.append((
            "fx_rate_applied", "SKIP",
            f"No FX required — instructed currency {instructed_currency} "
            f"matches debtor account currency {debtor_account_currency}.",
        ))
        return

    rate = SIMULATED_FX_RATES.get(
        (instructed_currency, debtor_account_currency),
        _DEFAULT_SIMULATED_RATE,
    )
    instructed_amount = payment.get("instructedAmount", 0)
    settlement_amount = round(instructed_amount * rate, 2)

    p._set("fxRate", rate, before=payment.get("fxRate"), source="simulated-fx-table")
    p._set("amount", settlement_amount, before=payment.get("amount"), source="simulated-fx")
    p._set("currency", debtor_account_currency,
           before=payment.get("currency"), source="simulated-fx")

    p.outcomes.append((
        "fx_rate_applied", "PASS",
        f"Simulated FX rate {rate} ({instructed_currency}→"
        f"{debtor_account_currency}) applied: {instructed_amount:,.2f} "
        f"{instructed_currency} → {settlement_amount:,.2f} "
        f"{debtor_account_currency}. Rate source: SIMULATED — no live FX "
        "provider in Phase 1.",
    ))


def _plan_regulatory_reports(p: EnrichmentPlan, payment: dict) -> None:
    """FR-3.12 — populate `correspondent.regulatoryReports[]` when the wire is
    cross-border or above the reporting threshold.

    The canonical spec declares the array (required, "Empty array if none") but no item
    schema, so the record shape here is temporary and awaits Doina's ratification (Q24). It
    mirrors the sibling `sanctionsCheck` record — a status, a timestamp, an authority — plus
    the trigger reason and `simulated: true`. The timestamp (`assessedAt`) is left null by the
    plan and stamped by `enrichment.run`, which owns the clock; same discipline as
    `payment_document.build` initialising fields null for a later stage to fill.

    Two triggers, both can fire on one payment:
      * **cross-border** — `wireDetails.wireType == "INTERNATIONAL"` (the stage-1 corridor
        determination, the same signal stage 4 routes on) → a `CROSS_BORDER_DECLARATION`.
      * **threshold** — `amount` ≥ `regulatory_report_threshold()` → a `THRESHOLD_REPORT`
        carrying the threshold that fired.

    Scoped to WIRE: a book transfer reaches no clearing system and attracts no regulatory
    report in this demo's posture. A domestic wire under the threshold keeps `[]` (the spec's
    "Empty array if none") and a SKIP — no `$set`, so the before/after diff stays clean.
    `wireType` is read in preference to `creditor.bankCountry` because the plan runs against
    the pre-enrichment document, and `bankCountry` is resolved by `_plan_creditor_bank` (a
    sibling, whose updates this helper cannot see); `wireType` was derived at stage 1.
    """
    rail = payment.get("rail")
    if rail != "WIRE":
        p.outcomes.append((
            "regulatory_reports_assessed", "SKIP",
            f"Regulatory reporting applies to wire transfers; rail {rail} carries none.",
        ))
        return

    reports: list[dict] = []

    wire_type = (payment.get("wireDetails") or {}).get("wireType")
    if wire_type == "INTERNATIONAL":
        creditor = payment.get("creditor") or {}
        reports.append({
            "reportType": _REPORT_TYPE_CROSS_BORDER,
            "authority": REGULATORY_AUTHORITY,
            "status": REGULATORY_REPORT_STATUS,
            "assessedAt": None,  # stamped by enrichment.run, which owns `now`
            "reason": (
                "Cross-border wire requires a regulatory declaration "
                f"(wireType {wire_type})."
            ),
            "thresholdAmount": None,
            "simulated": True,
        })

    amount = payment.get("amount") or 0
    threshold = regulatory_report_threshold()
    if amount >= threshold:
        reports.append({
            "reportType": _REPORT_TYPE_THRESHOLD,
            "authority": REGULATORY_AUTHORITY,
            "status": REGULATORY_REPORT_STATUS,
            "assessedAt": None,
            "reason": (
                f"Wire amount {amount:,.2f} {payment.get('currency', '')} meets or "
                f"exceeds the {threshold:,.0f} reporting threshold."
            ),
            "thresholdAmount": threshold,
            "simulated": True,
        })

    if not reports:
        p.outcomes.append((
            "regulatory_reports_assessed", "SKIP",
            f"Domestic wire below the {threshold:,.0f} reporting threshold — no "
            "regulatory report required.",
        ))
        return

    correspondent = payment.get("correspondent") or {}
    p._set(
        "correspondent.regulatoryReports", reports,
        before=correspondent.get("regulatoryReports", []),
        source="regulatory-reporting-rules",
    )
    p.outcomes.append((
        "regulatory_reports_assessed", "PASS",
        f"{len(reports)} regulatory report(s) attached: "
        + ", ".join(r["reportType"] for r in reports)
        + f". Authority {REGULATORY_AUTHORITY} — SIMULATED, no filing occurs in the demo.",
    ))


def snapshot_original(payment: dict, paths: list[str]) -> dict:
    """The pre-enrichment values of exactly the fields about to change (B1).

    Only the touched fields, kept as a nested dict mirroring the document, so the before/after
    panel can walk the same shape on both sides. Snapshotting the whole document would work
    too and would be simpler — but it doubles every payment's storage to answer a question
    about five fields.
    """
    original: dict = {}
    for path in paths:
        parts = path.split(".")
        source: Any = payment
        for part in parts:
            source = (source or {}).get(part) if isinstance(source, dict) else None
        target = original
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = source
    return original
