"""Is the chosen payment type reachable on the chosen rail? Doc 17 §3 step 3 (R10).

Doina L462: *"Since **paymentType** was already set in Stage 1, this stage validates that
the chosen type is actually viable for this payment (e.g. beneficiary bank reachable on that
rail) rather than selecting the type from scratch."*

That sentence names two different questions. This module answers the first — **is this
type/rail pairing coherent at all** — which is pure and needs no data. The second,
*beneficiary bank reachable on that rail*, needs the directory and lives in the enrichment
half; a `WIRE` to a bank we cannot resolve is a WARN, not a refusal (doc 17 B7).

Before this, `ctx.payment_type` was accepted and never compared to `ctx.payment_rail`, so
`rail: INTERNAL` with `type: CARD_PAYMENT` was storable — a document that satisfies the
schema (both values are in their enums) and describes nothing that can happen.

## The matrix

Both enums are **verbatim from the spec's `$jsonSchema`** (`payments.rail`, `payments.type`)
via `api_models.py`, per defect 2026-04-28 — never hand-rolled. `test_rail_viability` asserts
the keys and values against the spec file, so a spec change fails here rather than drifting.

Where the pairings come from:

- **INTERNAL** — a book transfer between two accounts we hold. `CREDIT_TRANSFER` is the
  normal case and is what the whole demo runs on; `STANDING_ORDER` is a recurring instruction
  that settles as one. `DIRECT_DEBIT` is excluded deliberately: a debit pulled by a creditor
  is an ACH construct, and the demo has no mandate model.
- **WIRE** — ISO 20022 credit transfer (`pain.001` → `pacs.008`). Credit push only; a wire
  cannot pull funds, which is the whole reason wires are irrevocable.
- **ACH** — `CREDIT_TRANSFER` and `DIRECT_DEBIT` both, since the SEC code carries the
  direction (her L211: *"the SEC code determines the transaction's characteristics"*).
  Phase 2, but the pairing is correct now.
- **CARD** — `CARD_PAYMENT` only. Nothing else runs over a card network.
- **RTP** — `RTP` (the instant-payment message) and `CREDIT_TRANSFER`, since FedNow and
  RTP/TCH carry ordinary credit transfers too.

`CHEQUE` and `INTRABANK_TRANSFER` are absent from both enums — stage 1 removed them, and
Doina Q10 asks her to confirm that `rail: INTERNAL` + `type: CREDIT_TRANSFER` is the intended
representation of what used to be `INTRABANK_TRANSFER`. This module encodes that answer; if
she says otherwise, only the `INTERNAL` row changes.
"""

from __future__ import annotations

from typing import Optional

# rail -> the payment types that rail can carry.
VIABLE_TYPES_BY_RAIL: dict[str, frozenset[str]] = {
    "INTERNAL": frozenset({"CREDIT_TRANSFER", "STANDING_ORDER"}),
    "WIRE": frozenset({"CREDIT_TRANSFER"}),
    "ACH": frozenset({"CREDIT_TRANSFER", "DIRECT_DEBIT"}),
    "CARD": frozenset({"CARD_PAYMENT"}),
    "RTP": frozenset({"RTP", "CREDIT_TRANSFER"}),
}

# Rails the demo can actually execute today. Phase 1 is wires + internal transfers; ACH and
# cards are Phase 2 and are `disabled` in the UI with a "Coming soon" label. Kept separate
# from the matrix above so the two facts do not get confused: a pairing can be *coherent*
# and still not *implemented*, and those deserve different messages.
PHASE_1_RAILS = frozenset({"INTERNAL", "WIRE"})


def viability_problem(rail: Optional[str], payment_type: Optional[str]) -> Optional[str]:
    """Why `payment_type` cannot travel on `rail`, or None.

    Returns a reason a caller can put straight into a `checks[]` `detail`. An unknown rail is
    reported rather than silently passed — the request contract's `Literal` should have
    caught it, and if it did not, that is worth knowing.
    """
    if not rail or not payment_type:
        return None

    viable = VIABLE_TYPES_BY_RAIL.get(rail)
    if viable is None:
        return f"Rail {rail!r} is not a known payment rail."

    if payment_type not in viable:
        return (
            f"Payment type {payment_type} is not viable on rail {rail} — "
            f"{rail} carries {', '.join(sorted(viable))}."
        )
    return None


def is_phase_1(rail: Optional[str]) -> bool:
    """Whether this rail is implemented end to end today.

    Not a validation rule: the UI already prevents choosing a Phase-2 rail, and refusing here
    would duplicate that in a second place. Used only to make a check's `detail` honest about
    what a pass means.
    """
    return rail in PHASE_1_RAILS
