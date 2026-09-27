"""Port for the phase-1 Enrichment Agent — the seam through which stage 3 reaches the AI.

`domain/enrichment.py` depends on this Protocol, never on the HTTP adapter (`adapters/`),
per the repo's one DDD rule: `domain/` may import `ports/`, never `adapters/`. The adapter
(`enrichment_agent_client.HttpEnrichmentAgent`) implements this port and is injected by
`PaymentsService` through the `PaymentContext`, exactly as `reference_data` and `rail_gateway`
are.

## Option B: propose, don't write

The agent returns proposals (`{field, to, reason, source}`); it never writes the payment.
`enrichment.run` applies the proposals it accepts through its existing atomic `$set`. A
null/unconfigured agent returns `[]` and the saga proceeds deterministically — the same
best-effort posture as `ReferenceData` (doc 17 B7).
"""

from __future__ import annotations

from typing import Protocol


class EnrichmentAgent(Protocol):
    """What stage 3 asks of the Enrichment Agent. Implementations must not raise."""

    def propose(self, payment_id: str, payment: dict) -> list[dict]:
        """Return enrichment proposals for the payment, possibly []. Never raises."""
        ...


class NullEnrichmentAgent:
    """Resolves no proposals. The safe default when no agent service is configured.

    A service constructed without an agent still runs the whole saga: enrichment proceeds
    with the deterministic planner only, identical to pre-phase-1 behaviour.
    """

    def propose(self, payment_id: str, payment: dict) -> list[dict]:
        return []
