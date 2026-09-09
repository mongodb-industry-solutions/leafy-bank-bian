"""Stage 3 — payment validation (BIAN PaymentOrderInitiation, SD 42933).

Doina stage 3, validation half (L434-L439, L462). Her question for this stage: *"Is the
payment valid, and is the money or credit actually available?"*

Reads  ctx: debtor_account, creditor_account, debtor_account_ref, creditor_account_ref,
            creditor_party, instructed_amount, instructed_currency, payment_type,
            payment_rail, is_external_creditor, payment_oid, current_state, collections
Writes ctx: current_state -> VALIDATED; ten `payments.checks[]` entries

## What changed in stage 3, and what did not

Most of these rules already ran — and every one of them passed or threw **silently**. Her
L925 asks for *"structured validation outcomes"*, so the substantive change is that each
rule now records a `checks[]` entry with an actor, a timestamp and a sync/async mode (R11,
R12). Two rules are genuinely new: identifier format (R8) and rail/type viability (R10).

The amount-vs-limit check stays in `capture.py` — Doina's Stage 1 covers basic field
validation and it needs no account data. The DEBTOR account-status check stays in stage 2
(doc 15 B6), broadened there to CLOSED / DORMANT / FROZEN plus `restrictions[]`: entitlement
to use the account is stage 2's question. The CREDITOR check below stays here — a payee's
account state is payment validity, not the payer's entitlement.

## Refuse here, warn later — with one exception

This module holds the rules that make a payment order invalid **on its face**, so each one
raises. A failure raises `ValueError`; the saga catches it, marks the payment REJECTED with
the failing stage as the reason (§11), and re-raises — this module does no rejection
bookkeeping of its own beyond flushing the trail first, so the demo can see *which* check
refused.

Anything that depends on **reference data** is deliberately not here. A creditor bank we
cannot resolve is a WARN in the enrichment half, never a refusal: one missing directory row
must not fail every wire (doc 17 B7, and the 2026-07-01 lesson about permanent vs transient
failure). The split is: `validation` refuses on the caller's error, `enrichment` warns on
ours, `final_validation` refuses on what enrichment failed to supply.

**One exception (FR-3.14):** `currency_consistent` WARNs on a currency mismatch rather
than refusing, because the payment can proceed with a SIMULATED fxRate attached at
enrichment. A currency mismatch is a caller's error that we choose to defer rather than
refuse — the international wire (a named core journey, L959) must not be blocked by it.

## Still to come in this stage

- Duplicate detection as a WARN check — step 4, deliberately without wiring an idempotency
  key (doc 17 B4, and the standing precondition on the deferred Atlas index).
- Beneficiary bank reachability on the rail, purpose-code resolution, routing data — the
  enrichment half, step 5.
"""

from __future__ import annotations

from datetime import datetime, timezone

from contexts.payment_order_initiation.domain import (
    bank_identity,
    checks,
    duplicate_detection,
    identifier_format,
    lifecycle,
    rail_viability,
)
from process.payment_context import PaymentContext

STAGE = "3 validate"


def run(ctx: PaymentContext) -> None:
    now = datetime.now(timezone.utc)
    recorded: list = []

    def record(name: str, result: str, detail: str, *, mode: str = checks.SYNC) -> None:
        recorded.append(
            checks.check(STAGE, name, result, mode=mode, detail=detail, at=now)
        )

    def refuse(name: str, detail: str) -> None:
        """Record the failing check, flush the trail, and raise.

        The flush must precede the raise: the saga catches `ValueError` and marks the payment
        REJECTED, so a check written after that point would never exist. Same shape as stage
        2's `authenticate.refuse`.
        """
        record(name, checks.FAIL, detail)
        _flush(ctx, recorded)
        raise ValueError(detail)

    debtor = ctx.debtor_account
    creditor = ctx.creditor_account
    external = ctx.is_external_creditor
    creditor_party = ctx.creditor_party or {}

    # --- 1. required_fields (R1) --------------------------------------------
    # Enforced at the request boundary by `PaymentOrderInitiateRequest` (Pydantic, with
    # `extra="forbid"` and a cross-field `model_validator`), so by the time the saga runs
    # this cannot fail. Recorded rather than re-checked: her L435 lists it as a visible
    # check, and a check the demo cannot see is not a check she asked for. Re-implementing
    # the rule here would be a second, drifting copy of the contract.
    record(
        "required_fields", checks.PASS,
        "Request satisfied the payment-order contract (required fields present, no "
        "unknown fields, envelope matches the chosen rail).",
    )

    # --- 2. currency_valid (R2) ---------------------------------------------
    # ISO-4217 shape is enforced in `capture.py` before the document exists.
    record(
        "currency_valid", checks.PASS,
        f"{ctx.instructed_currency} is an ISO-4217 alpha-3 code.",
    )

    # --- 3. amount_valid (R3) -----------------------------------------------
    record(
        "amount_valid", checks.PASS,
        f"{ctx.instructed_amount:,.2f} {ctx.instructed_currency} is a positive amount "
        "within the configured bound.",
    )

    # --- 4. duplicate_detection (R4) — NEW, WARN only -----------------------
    # Her L435 groups this with structural validation, so it sits here rather than at the
    # end. WARN, never refusal, and it wires NO idempotency key — see `duplicate_detection`
    # and doc 17 B4 for why that boundary matters.
    _record_duplicate(record, ctx, now)

    # --- 5. payment_type_viable (R10) — NEW, may refuse ---------------------
    # `ctx.payment_type` was previously accepted and never compared to the rail.
    problem = rail_viability.viability_problem(ctx.payment_rail, ctx.payment_type)
    if problem:
        refuse("payment_type_viable", problem)
    record(
        "payment_type_viable", checks.PASS,
        f"{ctx.payment_type} is viable on rail {ctx.payment_rail}"
        + ("." if rail_viability.is_phase_1(ctx.payment_rail)
           else f" — note {ctx.payment_rail} is not executable in Phase 1."),
    )

    # --- 6. currency_consistent (R2) ---------------------------------------
    # FR-3.14: a currency mismatch is a WARN, not a refusal — the payment proceeds
    # and enrichment attaches a SIMULATED fxRate when the instructed currency differs
    # from the debtor account currency. One outcome per check (if/elif/else).
    debtor_currency = debtor.get("currency")
    if debtor_currency != ctx.instructed_currency:
        record(
            "currency_consistent", checks.WARN,
            f"Debtor account is {debtor_currency}, instruction is "
            f"{ctx.instructed_currency} — FX conversion will be applied at a "
            "simulated rate during enrichment (FR-3.14).",
        )
    elif not external and creditor.get("currency") != debtor_currency:
        record(
            "currency_consistent", checks.WARN,
            f"Creditor account is {creditor.get('currency')}, debtor account is "
            f"{debtor_currency} — internal FX conversion will be applied at a "
            "simulated rate during enrichment (FR-3.14).",
        )
    else:
        record(
            "currency_consistent", checks.PASS,
            f"Debtor account currency {debtor_currency} matches the instruction"
            + ("" if external else " and the creditor account") + ".",
        )

    # --- 7. beneficiary_recognised (R7) ------------------------------------
    # An external beneficiary has no account here to inspect. Its status and currency are
    # the receiving bank's to police; whether its BANK is reachable is the enrichment half's
    # question, and a miss there is a WARN. Only the two-sided rules are skipped — every
    # debtor-side rule above still ran.
    if external:
        record(
            "beneficiary_recognised", checks.SKIP,
            f"Beneficiary {creditor_party.get('name') or 'unnamed'} is external "
            f"(account {creditor_party.get('accountNo')}); the bank holds no account to "
            "verify. Bank reachability is resolved during enrichment.",
        )
    else:
        if creditor.get("status") == "CLOSED":
            refuse("beneficiary_recognised", "Creditor account is CLOSED.")
        if ctx.debtor_account_ref == ctx.creditor_account_ref:
            refuse(
                "beneficiary_recognised",
                "Debtor and creditor accounts must differ.",
            )
        record(
            "beneficiary_recognised", checks.PASS,
            f"Creditor account {ctx.creditor_account_ref} is held by the bank and is "
            f"{creditor.get('status')}.",
        )

    # --- 8. account_format_valid (R8) — NEW, may refuse --------------------
    _validate_identifiers(refuse, record, ctx, external, creditor_party)

    # --- 9. domestic_or_crossborder (R9) -----------------------------------
    # Her L439: compare the beneficiary bank's country against our own. No flag is written —
    # "no separate domestic/cross-border flag is needed on the instruction itself" — and
    # `wireDetails.wireType`, already derived at stage 1, is the only record of the result.
    _record_corridor(record, ctx, external, creditor_party)

    # --- 10. funds_available (R6) -------------------------------------------
    # Pre-flight floor. Re-checked inside the ACID transaction in stage 5, which is the
    # check that actually holds — this one gives a clean 400 instead of a rollback.
    available = (debtor.get("balance", {}) or {}).get("available", 0)
    if available < ctx.instructed_amount:
        refuse(
            "funds_available",
            f"Insufficient available balance: {available:,.2f} "
            f"{debtor_currency} available, {ctx.instructed_amount:,.2f} required.",
        )
    record(
        "funds_available", checks.PASS,
        f"{available:,.2f} {debtor_currency} available covers "
        f"{ctx.instructed_amount:,.2f}. Re-checked atomically at settlement.",
    )

    _flush(ctx, recorded)

    lifecycle.advance_ctx(
        ctx, lifecycle.VALIDATED,
        actor="transactions-service",
        reason="Structural and account validation passed",
    )


def _record_duplicate(record, ctx, now) -> None:
    """R4 — does this payment resemble a recent one?

    Advisory only. The query is built by the domain module and executed here, which is what
    keeps the predicate unit-testable without a database.

    No sort: any match is enough for a warning, and depending on ordering would buy nothing
    but a stricter contract on the collection.
    """
    match = ctx.collections.payments.find_one(
        duplicate_detection.recent_duplicate_filter(
            debtor_account_id=ctx.debtor_account_ref,
            creditor_account_no=_creditor_account_no(ctx),
            instructed_amount=ctx.instructed_amount,
            instructed_currency=ctx.instructed_currency,
            now=now,
            exclude_payment_id=ctx.payment_id,
        )
    )
    if match:
        record("duplicate_detection", checks.WARN, duplicate_detection.describe(match, now=now))
        return
    record(
        "duplicate_detection", checks.PASS,
        f"No payment with the same debtor, beneficiary, amount and currency in the last "
        f"{duplicate_detection.window_seconds() // 60} minute(s).",
    )


def _creditor_account_no(ctx) -> str | None:
    """The beneficiary account number, whichever side of the internal/external split it is on.

    The payment document stores one `creditor.accountNo` either way, so the duplicate filter
    must resolve to the same value the document holds — otherwise an external payment could
    never match its own predecessor.
    """
    if ctx.is_external_creditor:
        return (ctx.creditor_party or {}).get("accountNo")
    return (ctx.creditor_account or {}).get("accountNumber")


def _validate_identifiers(refuse, record, ctx, external: bool, creditor_party: dict) -> None:
    """R8 — every identifier on the instruction is well-formed.

    Format only. Whether the institution exists is the directory's question, and the two
    outcomes differ on purpose: malformed refuses, unknown warns (doc 17 B7).

    Our own side is checked too. It is a constant (`bank_identity`), so it cannot fail in
    practice — but a typo in that constant would otherwise reach a rail message silently,
    and the check costs nothing.
    """
    problems: list[str] = []
    validated: list[str] = []

    for label, value, validator in (
        ("our BIC", bank_identity.OUR_BIC, identifier_format.bic_problem),
        ("our routing number", bank_identity.OUR_ABA, identifier_format.aba_problem),
        ("debtor IBAN", ctx.debtor_account.get("iban"), identifier_format.iban_problem),
    ):
        if not value:
            continue
        problem = validator(value)
        if problem:
            problems.append(problem)
        else:
            validated.append(label)

    if external:
        bic = creditor_party.get("bic")
        clearing_code = creditor_party.get("clearingSystemCode")
        member_id = creditor_party.get("clearingSystemMemberId")

        for label, value, problem in (
            ("beneficiary BIC", bic, identifier_format.bic_problem(bic)),
            ("beneficiary IBAN", creditor_party.get("iban"),
             identifier_format.iban_problem(creditor_party.get("iban"))),
        ):
            if not value:
                continue
            problems.append(problem) if problem else validated.append(label)

        if member_id:
            problem = identifier_format.clearing_member_problem(clearing_code, member_id)
            if problem:
                problems.append(problem)
            elif identifier_format.clearing_member_is_validated(clearing_code):
                validated.append(f"beneficiary {clearing_code} member id")
            else:
                # Honest about the gap rather than claiming a pass: only USABA has a
                # checksum rule implemented (see `identifier_format`).
                validated.append(
                    f"beneficiary {clearing_code} member id (format not validated for "
                    f"{clearing_code})"
                )
    else:
        iban = (ctx.creditor_account or {}).get("iban")
        if iban:
            problem = identifier_format.iban_problem(iban)
            problems.append(problem) if problem else validated.append("creditor IBAN")

    if problems:
        refuse("account_format_valid", " ".join(problems))
    record(
        "account_format_valid", checks.PASS,
        ("Validated " + ", ".join(validated) + ".") if validated
        else "No structured identifiers were supplied to validate.",
    )


def _record_corridor(record, ctx, external: bool, creditor_party: dict) -> None:
    """R9 — domestic vs cross-border corridor (FR-3.7, 4-category matrix L396-401).

    Determines 3 of the doc's 4 categories from data available today:
      - domestic-same-bank — creditor held by us (not external)
      - domestic-different-bank — external, beneficiary bank country == our country
      - cross-border — external, beneficiary bank country != our country

    The 4th split (cross-border-direct vs cross-border-intermediary) needs correspondent
    directory data we don't have (doc 17 B2), so it is DEFERRED and recorded as a
    single "cross-border" category with a note in the check detail.

    Records `validation.determinedCategory` as an audit snapshot (doc L404:
    "computed outcome snapshot, not new instruction data") directly on the payment —
    separate from the checks `$push` flush, so it survives even a later refusal.
    `wireType` (DOMESTIC/INTERNATIONAL) stays 2-value: it drives ISO 20022 message
    structure, not the corridor.
    """
    our_country = bank_identity.OUR_BANK_COUNTRY
    their_country = (
        creditor_party.get("bankCountry") if external else our_country
    )

    if not their_country:
        record(
            "domestic_or_crossborder", checks.SKIP,
            f"Beneficiary bank country is not stated, so the corridor cannot be "
            f"determined against our own ({our_country}). Resolved during enrichment if "
            "the beneficiary bank is in the directory.",
        )
        return

    # 3 of the doc's 4 categories determinable from data we have today (L396-401).
    # The 4th — cross-border-direct vs cross-border-intermediary — needs correspondent
    # directory data we don't have (doc 17 B2: correspondentBanks is JP/IN only),
    # so it is DEFERRED and recorded as a single "cross-border" category with a note.
    if not external:
        category = "domestic-same-bank"
        corridor = "DOMESTIC"
    elif their_country == our_country:
        category = "domestic-different-bank"
        corridor = "DOMESTIC"
    else:
        category = "cross-border"
        corridor = "INTERNATIONAL"

    # Audit snapshot (doc L404: "computed outcome snapshot, not new instruction data").
    # Written directly here — separate from the checks `$push` flush — because the
    # category is an audit snapshot, not a check, and should survive even a later
    # refusal (it records what was determined, regardless of outcome).
    ctx.collections.payments.update_one(
        {"_id": ctx.payment_oid},
        # Dotted path — `validation` is initialised `{}` so the parent exists. Setting only
        # the category leaves sibling fields intact when FR-3.9's overallStatus/
        # failureReasons[] land; a whole-object `$set` would have clobbered them.
        {"$set": {"validation.determinedCategory": category}},
    )

    record(
        "domestic_or_crossborder", checks.PASS,
        f"Beneficiary bank country {their_country} vs originating {our_country} — "
        f"{corridor}. Corridor category: {category}."
        + ("" if external else " Both accounts are held by the bank.")
        + (
            " Cross-border direct vs intermediary determination deferred — "
            "correspondent directory data not available."
            if category == "cross-border"
            else ""
        ),
    )


def _flush(ctx: PaymentContext, recorded: list) -> None:
    checks.append_checks(ctx.collections.payments, ctx.payment_oid, recorded)
    recorded.clear()
