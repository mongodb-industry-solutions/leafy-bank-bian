"""Stage 4a — payment orchestration.

BIAN PaymentOrchestration (SD 48782). Verified against the v14 landscape: the SD exists and
carries **no published semantic API**, so D8 governs its URL (`POST
/PaymentOrchestration/Initiate`) and the two documents it writes are ours to shape
(doc 18 B1, Q28).

Doina L487: orchestration decides **how** to execute within the rail the customer already
chose in stage 1 — never which rail. `rail` is read here and never written, and
`test_orchestration_never_changes_the_rail` holds that line (R1).

Reads  ctx: payment_doc, payment_rail, priority, instructed_amount/currency,
            requested_execution_date, reference_data, is_external_creditor,
            payment_oid, current_state, collections
Writes ctx: current_state -> ROUTED; execution_strategy, routing_snapshot_id, warehoused;
            `routingSnapshots` (insert-only); `payments.wireDetails.network`,
            `payments.correspondent.{correspondentBic,intermediaryBic}`,
            `payments.refs.routingSnapshotId`; six `payments.checks[]` entries

## What refuses here, and what warns

The stage-3 rule carries forward (doc 17 B7): **we refuse on the caller's error, warn on
ours.** Orchestration owns almost no caller error — by the time it runs, stage 3 has
already refused everything structurally wrong — so the only refusal here is a payment with
no viable execution path at all. A thin correspondent table, a missing directory row, a
missed cut-off: all WARN. One absent reference row must not fail every international wire
(the 2026-07-01 lesson on permanent vs transient failure).

## The warehouse halt

Her L536 Key Feature is *"Manage scheduled payments (Payment Warehousing - Release)"*. A
future-dated payment is routed, recorded, and then **held at ROUTED** via `ctx.stop()` —
the same mechanism stage 5's external-creditor halt uses. The release scheduler is
deliberately deferred (doc 18 B9): a new interval worker is the most defect-prone surface
in this repo, and the hold is what makes the captured date mean something.

⚠️ **A future-dated payment therefore never reaches the GL.** The manual gate must use a
same-day `requestedExecutionDate`, or it will look like a regression.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from contexts.payment_order_initiation.domain import checks, lifecycle
from contexts.payment_orchestration.domain import documents, routing
from process.payment_context import PaymentContext

STAGE = "4 orchestrate"

# US Eastern, as a fixed offset. Deliberately not `zoneinfo`: the cut-off table is a demo
# constant (routing.py), so a DST-correct clock would be false precision on top of an
# approximation. It also keeps this module free of a tzdata dependency.
_ET_OFFSET_HOURS = -4


def run(ctx: PaymentContext) -> None:
    now = datetime.now(timezone.utc)
    recorded: list = []
    payment = ctx.payment_doc or {}

    def record(name: str, result: str, detail: str, *, mode: str = checks.SYNC) -> None:
        recorded.append(
            checks.check(STAGE, name, result, mode=mode, detail=detail,
                         actor="orchestration-service", at=now)
        )

    def refuse(name: str, detail: str) -> None:
        """Record, flush, then raise — the flush must precede the raise.

        The saga catches `ValueError` and marks the payment REJECTED, so a check written
        after that point would never exist. Same shape as stage 2's `authenticate.refuse`
        and stage 3's `validation.refuse`.
        """
        record(name, checks.FAIL, detail)
        _flush(ctx, recorded)
        raise ValueError(detail)

    creditor = payment.get("creditor") or {}
    wire = payment.get("wireDetails") or {}

    # --- 1. execution_strategy_selected (R2, R3) -----------------------------
    strategy = routing.decide(
        rail=ctx.payment_rail,
        wire_type=wire.get("wireType"),
        priority=payment.get("priority") or ctx.priority,
        amount=payment.get("amount") or ctx.instructed_amount,
        currency=payment.get("currency") or ctx.instructed_currency,
        creditor_country=creditor.get("bankCountry"),
        creditor_bic=creditor.get("bic"),
        requested_execution_date=ctx.requested_execution_date,
        now=now.date(),
        now_hour_et=(now + timedelta(hours=_ET_OFFSET_HOURS)).hour,
    )
    ctx.execution_strategy = strategy

    if strategy.strategy is None:  # pragma: no cover - `decide` is total by construction
        refuse(
            "execution_strategy_selected",
            f"No execution strategy exists for rail {ctx.payment_rail}.",
        )
    record(
        "execution_strategy_selected", checks.PASS,
        f"{strategy.strategy} at {strategy.cost_rank} relative cost. {strategy.rationale}",
    )

    # --- 2. clearing_network_selected (R25) ----------------------------------
    # `wireDetails.network` is wire-only, and the contract already reserves it for this
    # stage: `api_models.py` rejects a caller-supplied value as "a routing decision made in
    # stage 4". This is where that promise is kept.
    updates: dict = {}
    if strategy.network:
        updates["wireDetails.network"] = strategy.network
        record(
            "clearing_network_selected", checks.PASS,
            f"{strategy.network} selected for a {wire.get('wireType') or ctx.payment_rail} "
            f"payment. Value date {strategy.value_date}.",
        )
    else:
        record(
            "clearing_network_selected", checks.SKIP,
            f"Rail {ctx.payment_rail} addresses no clearing network — "
            f"{strategy.strategy}.",
        )

    # --- 3. correspondent_resolved (R26) — WARN on a miss, never a refusal ---
    resolved_correspondent = _resolve_correspondent(ctx, strategy, record)
    if strategy.correspondent_bic:
        updates["correspondent.correspondentBic"] = strategy.correspondent_bic
        # An intermediary hop exists only when our correspondent is NOT the beneficiary's
        # own bank. When they are the same institution the payment lands in one hop, and
        # stamping an intermediary would describe a hop that does not happen.
        if (creditor.get("bic") or "").upper() != strategy.correspondent_bic.upper():
            updates["correspondent.intermediaryBic"] = strategy.correspondent_bic

    # --- 4. within_cutoff (R2's cut-off input) -------------------------------
    if strategy.cutoff_hour_et is None:
        record(
            "within_cutoff", checks.SKIP,
            f"{strategy.network or strategy.strategy} has no cut-off window.",
        )
    elif strategy.within_cutoff:
        record(
            "within_cutoff", checks.PASS,
            f"Submitted before the {strategy.cutoff_hour_et:02d}:00 ET "
            f"{strategy.network} cut-off; value date {strategy.value_date}.",
        )
    else:
        record(
            "within_cutoff", checks.WARN,
            f"Past the {strategy.cutoff_hour_et:02d}:00 ET {strategy.network} cut-off — "
            f"value date rolls to {strategy.value_date}.",
        )

    # --- 5. routing_snapshot_written (R7) -----------------------------------
    snapshot = documents.routing_snapshot(
        payment=payment,
        strategy=strategy,
        resolved_correspondent=resolved_correspondent,
        now=now,
    )
    _insert_snapshot(ctx, snapshot)
    ctx.routing_snapshot_id = snapshot["routingSnapshotId"]
    updates["refs.routingSnapshotId"] = snapshot["routingSnapshotId"]
    record(
        "routing_snapshot_written", checks.PASS,
        f"{snapshot['routingSnapshotId']} records the network, the beneficiary agent and "
        "the correspondent as selected at this moment. Immutable — never updated.",
    )

    # --- 6. warehoused_for_release (R18) ------------------------------------
    ctx.warehoused = _is_future_dated(ctx, now)
    if ctx.warehoused:
        record(
            "warehoused_for_release", checks.PASS, mode=checks.ASYNC,
            detail=(
                f"Requested execution date {ctx.requested_execution_date} is in the "
                "future — the payment is routed and warehoused, awaiting release. "
                "Release is not automated in Phase 1 (doc 18 B9)."
            ),
        )
    else:
        record(
            "warehoused_for_release", checks.SKIP,
            "Same-day execution — no warehousing.",
        )

    _flush(ctx, recorded)

    payment_doc = lifecycle.advance_ctx(
        ctx, lifecycle.ROUTED,
        actor="orchestration-service",
        reason=(
            f"Execution path resolved within the {ctx.payment_rail} rail: "
            f"{strategy.strategy}"
            + (f" via {strategy.network}" if strategy.network else "")
        ),
        extra=updates,
    )

    if ctx.warehoused:
        # Held here on purpose. Authorization (4b) is what commits the bank, and committing
        # to execute a payment days before its value date would be a fiction.
        ctx.stop(payment_doc)


# --------------------------------------------------------------------------- #

def _resolve_correspondent(ctx, strategy, record):
    """Look the chosen correspondent up in the directory. A miss is a WARN.

    The routing decision is made without this lookup — `routing.decide` is pure and picks
    the correspondent from its own (SIMULATED) table. This call *verifies* the choice
    against `correspondentBanks` and enriches the snapshot with the resolved row, so a
    snapshot is self-contained. That ordering matters: routing must not fail because a
    directory row is missing.
    """
    if not strategy.requires_correspondent:
        record(
            "correspondent_resolved", checks.SKIP,
            f"{strategy.strategy} needs no correspondent — "
            f"{'domestic' if strategy.network else 'intrabank'} settlement.",
        )
        return None

    if not strategy.correspondent_bic:
        record(
            "correspondent_resolved", checks.WARN,
            "No correspondent is configured for this destination country. SWIFT was "
            "selected without a named correspondent; the routing snapshot records the "
            "gap rather than inventing a relationship.",
        )
        return None

    bank = ctx.reference_data.bank_by_bic(strategy.correspondent_bic)
    if bank is None:
        record(
            "correspondent_resolved", checks.WARN,
            f"Correspondent {strategy.correspondent_bic} is not in the bank directory. "
            "The routing decision stands; the snapshot records it as unresolved.",
        )
        return None

    record(
        "correspondent_resolved", checks.PASS,
        f"{strategy.correspondent_bic} ({bank.bank_name}, {bank.bank_country}) resolved "
        "from the bank directory. SIMULATED correspondent relationship — no nostro "
        "account data exists in the model (Q28).",
    )
    return {"bankName": bank.bank_name, "country": bank.bank_country}


def _insert_snapshot(ctx, snapshot: dict) -> None:
    """Insert-only. There is no update path to this collection, by design (documents.py)."""
    collection = _collection(ctx, "routing_snapshots", documents.ROUTING_SNAPSHOTS)
    if collection is None:  # pragma: no cover - only a hand-built context lacks it
        return
    collection.insert_one(snapshot)


def _collection(ctx, attr: str, name: str):
    """The named handle, falling back to `db[name]`.

    A context built before stage 4 existed carries neither handle; reaching through `db`
    keeps such a context working rather than raising an AttributeError deep in a stage.
    """
    collections = ctx.collections
    if collections is None:
        return None
    handle = getattr(collections, attr, None)
    if handle is not None:
        return handle
    db = getattr(collections, "db", None)
    return None if db is None else db[name]


def _is_future_dated(ctx, now: datetime) -> bool:
    requested = ctx.requested_execution_date
    return bool(requested and requested > now.date())


def _flush(ctx: PaymentContext, recorded: list) -> None:
    checks.append_checks(ctx.collections.payments, ctx.payment_oid, recorded)
    recorded.clear()
