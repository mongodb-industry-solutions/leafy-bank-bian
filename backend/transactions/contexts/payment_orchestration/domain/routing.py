"""Stage 4a — the execution-strategy decision. Pure.

BIAN PaymentOrchestration (SD 48782 — no published semantic API, so D8 governs the URL and
this shape is ours; doc 18 B1, Doina Q28).

Doina L487: *"Because the customer already selected the rail in Stage 1, orchestration's job
here is to determine **how** to execute within that rail, not which rail to use."* So `rail`
is an **input** here and never an output — `test_orchestration_never_changes_the_rail` holds
that line, and R1 is the whole reason this module returns a `network` rather than a `rail`.

Her L489 lists seven inputs: *"urgency · amount · destination · currency · customer
preference · cost · cut-off time"*. Four exist on the payment (`priority`, `amount`,
destination via `creditor.bic`/country, `currency`); **cost** and **cut-off** are decided
here, from the tables below; **customer preference** has no home anywhere in the model and is
deferred with a reason (doc 18 B5, Q32). Inventing a preference field would be the
2026-04-24 `bian-mapping` mistake.

No I/O, no clock, no `ctx`: `decide()` takes values and returns a dataclass. The one lookup
that needs a database — does the chosen correspondent actually exist in the directory — is
the caller's job, through the `ReferenceData` port. Deciding *that* a correspondent is
needed is a routing decision; resolving *which* row backs it is reference data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

# --- the enums we may emit ----------------------------------------------------
# Sourced from the spec, never hand-rolled (defect 2026-04-28). `wireDetails.network`:
#   ["FEDWIRE", "CHIPS", "SWIFT", "LYNX", "SEPA"]
# `test_every_network_this_module_can_emit_is_in_the_spec_enum` asserts it against the file
# rather than trusting this comment.
FEDWIRE = "FEDWIRE"
CHIPS = "CHIPS"
SWIFT = "SWIFT"

# Strategy labels. Not a spec enum — nothing in the canonical model names an execution
# strategy (doc 18 B1) — so these live here and are recorded on `routingSnapshots`.
BOOK_TRANSFER = "BOOK_TRANSFER"
FEDWIRE_RTGS = "FEDWIRE_RTGS"
CHIPS_NETTED = "CHIPS_NETTED"
SWIFT_CORRESPONDENT = "SWIFT_CORRESPONDENT"
ACH_STANDARD_WINDOW = "ACH_STANDARD_WINDOW"
RTP_INSTANT = "RTP_INSTANT"

# Relative cost, not a price. Phase 1 has no pricing service and inventing one would be a
# fabricated number in front of an audience; a rank is defensible and still lets
# orchestration say *why* it chose (her L489 lists cost as an input).
COST_LOW = "LOW"
COST_MEDIUM = "MEDIUM"
COST_HIGH = "HIGH"

URGENT_PRIORITIES = frozenset({"URGENT", "HIGH"})

# Demo cut-off times, in US Eastern, expressed as an hour for legibility. Real hubs hold a
# per-network calendar; this is a constant table and says so. `None` = no cut-off (a book
# transfer posts on our own ledger; SWIFT store-and-forward accepts around the clock).
_CUTOFF_HOUR_ET = {
    FEDWIRE: 18,   # Fedwire Funds Service closes 18:00 ET for customer transfers
    CHIPS: 17,     # CHIPS stops accepting new payment orders late afternoon
    SWIFT: None,
}

# Our nostro-holding agent in each destination country. These BICs MUST exist in the seeded
# `correspondentBanks` BIC_DIRECTORY, and
# `test_every_correspondent_resolves_against_the_bank_seed` asserts exactly that — the same
# parity-test pattern the frontend autofill pool uses, so the table cannot drift from the
# directory it names.
#
# ⚠️ SIMULATED. No correspondent-relationship or nostro-account data exists anywhere in the
# canonical model (doc 18 B5: zero matches for `nostro` / `settlementAccount` across the
# whole `payments` tree), so this is a demo table, labelled as one in every rationale it
# produces. Q28 asks Doina to ratify it alongside the `routingSnapshots` shape.
_CORRESPONDENT_BY_COUNTRY = {
    "GB": "BARCGB22",
    "DE": "DEUTDEFF",
    "CH": "UBSWCHZH",
    "CA": "ROYCCAT2",
}


@dataclass(frozen=True)
class ExecutionStrategy:
    """How this payment will be executed, within the rail the customer already chose.

    `network` is `None` for every rail except WIRE: `wireDetails.network` is a wire-only
    field, and stamping a clearing network onto a book transfer would be inventing
    interbank addressing for a payment that has none — the same reasoning that gated
    `INTERBANK_RAILS` in stage 3's enrichment.
    """

    strategy: str
    network: Optional[str]
    cost_rank: str
    requires_correspondent: bool
    correspondent_bic: Optional[str]
    cutoff_hour_et: Optional[int]
    within_cutoff: bool
    value_date: Optional[str]
    rationale: str


def decide(
    *,
    rail: str,
    wire_type: Optional[str],
    priority: str,
    amount: float,
    currency: str,
    creditor_country: Optional[str],
    creditor_bic: Optional[str],
    requested_execution_date: Optional[date],
    now: date,
    now_hour_et: int,
) -> ExecutionStrategy:
    """Pick the execution strategy. Total: every rail returns something.

    `now_hour_et` is passed in rather than read from a clock so the cut-off branch is
    testable without freezing time.
    """
    urgent = priority in URGENT_PRIORITIES

    if rail == "WIRE":
        strategy, network, cost, rationale = _wire(
            wire_type=wire_type, urgent=urgent, amount=amount, currency=currency
        )
    elif rail == "ACH":
        # Phase 2. The row exists so the shape is right and so an ACH payment is not
        # silently unrouted; Same Day ACH window selection is deferred (doc 18 R4/§6).
        strategy, network, cost = ACH_STANDARD_WINDOW, None, COST_LOW
        rationale = (
            "ACH standard batch window. Same Day ACH selection is Phase 2 — ACH is not in "
            "Doina's Phase 1 scope (L336)."
        )
    elif rail == "RTP":
        strategy, network, cost = RTP_INSTANT, None, COST_MEDIUM
        rationale = "Instant rail; the network lives on `rtp.network`, not `wireDetails`."
    else:
        # INTERNAL, and anything else the rail enum grows. A book transfer between two
        # accounts we hold reaches no clearing network at all.
        strategy, network, cost = BOOK_TRANSFER, None, COST_LOW
        rationale = (
            "Both accounts are held at Leafy Bank, so the payment posts on our own ledger "
            "and reaches no clearing system."
        )

    correspondent_bic = None
    requires_correspondent = network == SWIFT
    if requires_correspondent:
        correspondent_bic = _CORRESPONDENT_BY_COUNTRY.get((creditor_country or "").upper())
        if correspondent_bic:
            same = bool(creditor_bic) and correspondent_bic.upper() == creditor_bic.upper()
            rationale += (
                f" The beneficiary bank is our direct correspondent in "
                f"{creditor_country}, so no intermediary hop is required."
                if same
                else f" Routed through our {creditor_country} correspondent "
                     f"{correspondent_bic} as intermediary."
            )
        else:
            # Not a refusal. A thin correspondent table must not fail every international
            # wire — the caller records a WARN (doc 17 B7's rule, carried forward).
            where = creditor_country or "the destination country"
            rationale += (
                f" No correspondent is configured for {where}; SWIFT selected without a "
                "named correspondent."
            )

    cutoff_hour = _CUTOFF_HOUR_ET.get(network) if network else None
    within_cutoff = cutoff_hour is None or now_hour_et < cutoff_hour

    return ExecutionStrategy(
        strategy=strategy,
        network=network,
        cost_rank=cost,
        requires_correspondent=requires_correspondent,
        correspondent_bic=correspondent_bic,
        cutoff_hour_et=cutoff_hour,
        within_cutoff=within_cutoff,
        value_date=_value_date(
            requested_execution_date, now=now, within_cutoff=within_cutoff
        ),
        rationale=rationale,
    )


def _wire(*, wire_type, urgent, amount, currency):
    """The WIRE rows. Her L492 is the first one, and it is a table row, not a branch."""
    if (wire_type or "").upper() == "INTERNATIONAL":
        return (
            SWIFT_CORRESPONDENT, SWIFT, COST_HIGH,
            f"{amount:,.2f} {currency} cross-border and "
            f"{'urgent' if urgent else 'non-urgent'} — routed via SWIFT correspondent "
            "rather than a slower bilateral arrangement.",
        )
    if urgent:
        return (
            FEDWIRE_RTGS, FEDWIRE, COST_HIGH,
            "Domestic and urgent — Fedwire real-time gross settlement, which settles "
            "immediately at a higher cost than netted clearing.",
        )
    return (
        CHIPS_NETTED, CHIPS, COST_MEDIUM,
        "Domestic and non-urgent — CHIPS netted clearing, cheaper than Fedwire RTGS for a "
        "payment with no same-hour requirement.",
    )


def _value_date(requested: Optional[date], *, now: date, within_cutoff: bool) -> Optional[str]:
    """ISO date the funds are expected to be available to the beneficiary.

    Past the network's cut-off, value moves to the next day. ⚠️ Calendar-naive: no weekend
    or holiday calendar exists in this repo, and inventing one would be a fabricated
    business date. A real hub uses the network's published calendar.
    """
    base = requested or now
    if base <= now and not within_cutoff:
        base = now + timedelta(days=1)
    return base.isoformat()


def correspondent_countries() -> frozenset:
    """The countries the SIMULATED correspondent table covers. For tests and the UI."""
    return frozenset(_CORRESPONDENT_BY_COUNTRY)


def correspondent_bics() -> frozenset:
    """Every BIC the table can emit — asserted against the bank seed by a parity test."""
    return frozenset(_CORRESPONDENT_BY_COUNTRY.values())


def networks_emitted() -> frozenset:
    """Every `wireDetails.network` value this module can produce."""
    return frozenset({FEDWIRE, CHIPS, SWIFT})
