"""The outbound rail port — one interface, one implementation per rail.

Doina's L544 pipeline ends *"-> Payment Rail -> Clearing / RTGS / Correspondent"*, and doc 09
§3 makes the structural claim: **three implementations of one port** (wire, ACH, card) is what
the canonical-envelope premise looks like in code. *"If ACH ends up needing its own context,
the envelope has failed."* Phase 1 ships the port and the wire adapter; ACH and card are
unreachable today (`rail_viability.PHASE_1_RAILS`), so a second implementation would be
untestable scaffolding.

The port is deliberately thin: a message in, an acknowledgement out. It knows nothing about
`payments`, `PaymentContext` or Mongo — which is what lets the rail move behind HTTP, a queue
or a real gateway later without the stage changing.

`NullRailGateway` is the default on `PaymentContext`, mirroring `NullReferenceData`: a context
built without a gateway still runs the whole saga (an internal transfer reaches no rail), and a
rail-bound payment gets an explicit, honest refusal rather than an `AttributeError` deep inside
a stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class RailAck:
    """A rail's answer to one submission.

    Her L257-258 asks that we keep the *"original rail status, reason code, and message
    reference"* alongside our own normalised status — this is that half of the pair, and
    `execution_documents.acknowledge` is what writes it down.
    """

    accepted: bool
    network_ref: Optional[str] = None
    network_code: Optional[str] = None
    status_code: Optional[str] = None
    reason: Optional[str] = None
    message_ref: Optional[str] = None
    settlement_date: Optional[str] = None
    simulated: bool = True


class RailGateway(Protocol):
    """Submit one ISO 20022 (or rail-native) message. Returns the rail's acknowledgement."""

    def submit(self, message: dict, *, network: Optional[str],
               payment_id: str) -> RailAck: ...


class NullRailGateway:
    """Refuses every submission, and says why.

    Not a silent no-op: a payment that reached the rail boundary with no gateway configured
    must not look acknowledged. The refusal is a `RailAck(accepted=False)` rather than an
    exception, so the stage records a FAILED attempt through the ordinary path instead of the
    saga's rejection path — an infrastructure gap is not the caller's error (doc 17 B7).
    """

    def submit(self, message: dict, *, network: Optional[str],
               payment_id: str) -> RailAck:
        return RailAck(
            accepted=False,
            status_code="RJCT",
            reason="No rail gateway is configured for this service.",
        )
