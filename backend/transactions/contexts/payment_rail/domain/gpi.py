"""SWIFT gpi tracking, simulated — pure (GPI plan §3/§4).

A wire's path is the `routingSnapshots.route[]` stage 4 recorded: debtor agent → sender
correspondent → intermediary → creditor agent. Each hop reports one status to the tracker,
so up to the final status the hop's `seq` IS the event's `seq`:

* hop k (k < N) forwards to hop k+1: `ACSP`, `G000` when hop k+1 is a gpi member, else `G001`;
* the creditor agent (hop N) credits the beneficiary: `ACCC`;
* a hop m that refuses the payment before settlement: `RJCT` with an ISO reason code, in place
  of its ACSP/ACCC — outcome REJECTED.

REJECTED vs RETURNED: REJECTED is a refusal **before** settlement/credit, RETURNED a return
**after** it. A return follows the creditor agent's ACCC, so it is a second report from that
hop with event seq N+1 (`_id` `…:GPI:{N+1}`); any later status is appended past the last
stored seq the same way (`gpi_tracking.emit`), never colliding with a stored report.

`fromBic → toBic` is the message leg the status concerns: the outgoing leg for an ACSP, the
incoming leg for a final ACCC/RJCT.

Deterministic `seq` from the route gives a deterministic `_id` (`{paymentId}:GPI:{seq}`), so
every hook can emit the whole script up to its own point and the duplicate-key guard drops
what an earlier hook already wrote. No counter on the payment.

Stored in `paymentEvents` beside `EVENT`/`CHECK` (same envelope as `lifecycle_history`), and
insert-only like them. Not in the canonical spec yet — umbrella `/bump-data-model` follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from contexts.payment_orchestration.domain import routing
from contexts.payment_rail.domain import pacs008

KIND = "GPI"

# Directions — the payment's, not a message's.
OUTBOUND = "OUTBOUND"
INBOUND = "INBOUND"
DIRECTIONS = (OUTBOUND, INBOUND)

# gpi transaction statuses (ISO 20022 ExternalPaymentTransactionStatus1Code subset).
ACSP = "ACSP"  # accepted, settlement in process — forwarded
ACCC = "ACCC"  # accepted, credited to the creditor's account
RJCT = "RJCT"  # rejected (before settlement) / returned (after credit)
GPI_STATUSES = (ACSP, ACCC, RJCT)
FINAL_STATUSES = frozenset({ACCC, RJCT})

# gpi reason codes for ACSP. RJCT carries an ISO reason code instead.
G000 = "G000"  # forwarded to the next gpi agent
G001 = "G001"  # forwarded to the next non-gpi agent

# ISO reason codes the simulation uses on RJCT.
REASON_ACCOUNT_CLOSED = "AC04"   # settlement EXCEPTION at the beneficiary bank
REASON_INVALID_FORMAT = "FF01"   # rail NACK at submission

# Final outcomes — also the terminal tracking statuses.
CREDITED = "CREDITED"
REJECTED = "REJECTED"
RETURNED = "RETURNED"
OUTCOMES = (CREDITED, REJECTED, RETURNED)

# `payments.tracking.trackingStatus`. A missing `tracking` reads as NOT_TRACKED.
NOT_TRACKED = "NOT_TRACKED"
SENT = "SENT"
IN_TRANSIT = "IN_TRANSIT"
TRACKING_STATUSES = (NOT_TRACKED, SENT, IN_TRANSIT, CREDITED, REJECTED, RETURNED)

HOP_ROLES = (routing.DEBTOR_AGENT, routing.SENDER_CORRESPONDENT, routing.INTERMEDIARY,
             routing.CREDITOR_AGENT)

# Spacing between simulated intermediate confirmations.
HOP_SPACING_SECONDS = 5


@dataclass(frozen=True)
class Step:
    """One hop's status report, before it is timestamped and stored."""

    seq: int
    hop: dict
    from_bic: Optional[str]
    to_bic: Optional[str]
    gpi_status: str
    reason_code: Optional[str]
    outcome: Optional[str] = None


def script(route: list, outcome: Optional[str] = None, *, reject_at: Optional[int] = None,
           reason_code: Optional[str] = None) -> list:
    """The ordered status reports for `route`, ending at `outcome`.

    * `None` — still in flight: every forwarding hop's ACSP, nothing final.
    * `CREDITED` — the ACSPs, then the creditor agent's ACCC.
    * `REJECTED` — ACSPs up to `reject_at` (default the creditor agent), whose report is RJCT
      with `reason_code`. Nothing was credited.
    * `RETURNED` — the credited script, then the creditor agent's RJCT at seq N+1, on the
      leg back to the agent that sent it. Credited first, returned after.

    Callers slice the result to the steps their hook owns; seq is stable whatever they pick.
    """
    hops = sorted(route or [], key=lambda h: h["seq"])
    if not hops:
        return []
    last = len(hops)
    if outcome == RETURNED:
        steps = script(route, CREDITED)
        creditor = hops[-1]
        back_to = hops[-2]["bic"] if last > 1 else None
        return steps + [Step(last + 1, creditor, creditor["bic"], back_to, RJCT,
                             reason_code or REASON_ACCOUNT_CLOSED, RETURNED)]
    stop = last if outcome is None or outcome == CREDITED else (reject_at or last)
    steps = []
    for i, hop in enumerate(hops[:stop], start=1):
        prev_bic = hops[i - 2]["bic"] if i > 1 else None
        if outcome == REJECTED and i == stop:
            steps.append(Step(i, hop, prev_bic, hop["bic"], RJCT,
                              reason_code or REASON_ACCOUNT_CLOSED, REJECTED))
        elif i == last:
            if outcome == CREDITED:
                # ACCC carries no reason: G000/G001 describe a forward, and nothing follows.
                steps.append(Step(i, hop, prev_bic, hop["bic"], ACCC, None, CREDITED))
        else:
            nxt = hops[i]
            steps.append(Step(i, hop, hop["bic"], nxt["bic"], ACSP,
                              G000 if nxt.get("gpiMember") else G001))
    return steps


def inbound_route(payment: dict, our_bic: str, our_country: str) -> list:
    """The two-node path of a received wire: the sender's bank → us. Only our own report is
    ever emitted (decision 3), so the sender's hop is context for `fromBic`."""
    debtor = payment.get("debtor") or {}
    return [
        {"seq": 1, "role": routing.DEBTOR_AGENT, "bic": debtor.get("bic"),
         "country": debtor.get("bankCountry"), "gpiMember": False},
        {"seq": 2, "role": routing.CREDITOR_AGENT, "bic": our_bic, "country": our_country,
         "gpiMember": True},
    ]


def is_reportable(step: Step) -> bool:
    """The filter: a gpi member's report, or any final status (the tracker needs the end)."""
    return bool(step.hop.get("gpiMember")) or step.gpi_status in FINAL_STATUSES


def stamp(base: datetime, k: int) -> datetime:
    """The k-th forward after the first report at `base`, `k·spacing` later.

    Not capped at the emitting hook's clock: stage 5 and stage 7 run milliseconds apart, so a
    cap would collapse the hops onto one instant. The final status is stamped after the last
    hop instead (`gpi_tracking`), so the story stays ordered and ends at the final report.
    """
    return base + timedelta(seconds=HOP_SPACING_SECONDS * k)


def event_document(payment: dict, step: Step, *, at: datetime, direction: str) -> dict:
    """A `paymentEvents` document for one step. Envelope as `lifecycle_history`'s."""
    hop = step.hop
    return {
        "_id": f"{payment['paymentId']}:{KIND}:{step.seq}",
        "paymentId": payment["paymentId"],
        "rail": payment.get("rail"),
        "kind": KIND,
        "seq": step.seq,
        "at": at,
        # Stored bare (ISO form) so the tracker query is one value whatever prefix the
        # payment's own field still carries.
        # Lowercase: an inbound sender may write the UUID in capitals; the tracker query
        # normalises the same way.
        "uetr": (pacs008.iso_uetr(payment.get("uetr")) or "").lower(),
        "direction": direction,
        "hop": {"seq": hop["seq"], "role": hop["role"], "bic": hop.get("bic"),
                "country": hop.get("country")},
        "fromBic": step.from_bic,
        "toBic": step.to_bic,
        "gpiStatus": step.gpi_status,
        "reasonCode": step.reason_code,
        "outcome": step.outcome,
        "reportable": is_reportable(step),
        "simulated": True,
    }


def summary(events: list, hops_total: int) -> Optional[dict]:
    """`payments.tracking` from the payment's stored GPI events (the source of truth)."""
    if not events:
        return None
    latest = max(events, key=lambda e: e["seq"])
    if latest.get("outcome"):
        status = latest["outcome"]
    elif latest["hop"]["role"] == routing.DEBTOR_AGENT:
        status = SENT
    else:
        status = IN_TRANSIT
    return {
        "trackingStatus": status,
        "gpiStatus": latest["gpiStatus"],
        "reasonCode": latest["reasonCode"],
        "lastBic": latest["hop"]["bic"],
        "lastUpdatedAt": latest["at"],
        # Hops that passed it on or credited it; a refusing hop is not a confirmation.
        "hopsConfirmed": min(len({e["hop"]["seq"] for e in events if e["gpiStatus"] != RJCT}),
                             hops_total),
        "hopsTotal": hops_total,
        "outcome": latest.get("outcome"),
    }
