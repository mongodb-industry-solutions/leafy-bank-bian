"""HTTP client for the separate payment-agent service (phase-1 Enrichment Agent).

The transactions service stays thin: it does NOT carry the LangGraph / Bedrock / voyageai
stack. It calls the agent service synchronously at the Stage-3 gate and applies the proposals
it accepts (option B — the agent only proposes, never writes). Uses stdlib `urllib` so no new
dependency lands on the money-path service.

## Best-effort, never blocks

`AGENTS_BACKEND_URL` unset, the agent service down, a timeout, or any error → returns `[]`.
`enrichment.run` treats `[]` as "agent had nothing to add" and proceeds deterministically.
This is the same posture as the reference-data port (doc 17 B7): an agent outage is a miss,
not a reason to fail a payment. The timeout (30s) accommodates a cold LangGraph agent that
makes two Bedrock Haiku calls plus reference-data/vector-search lookups — a 5s timeout timed
out on the first cold call every time, silently dropping the agent block.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0


def _snapshot_for_agent(payment: dict) -> dict:
    """Trim the payment to the fields the agent reasons over.

    The agent only proposes a purpose code, so it needs the remittance text, any supplied
    purpose/category, and enough context (rail, amount, currency, parties) to judge fit.
    Sending the whole document would work but would put customer PII on the wire to a service
    that doesn't need most of it.
    """
    remittance = payment.get("remittance") or {}
    return {
        "paymentId": payment.get("paymentId"),
        "rail": payment.get("rail"),
        "amount": payment.get("amount"),
        "currency": payment.get("currency"),
        "categoryPurpose": payment.get("categoryPurpose"),
        "remittance": {
            "purposeCode": remittance.get("purposeCode"),
            "unstructured": remittance.get("unstructured"),
        },
        "creditor": {
            "name": (payment.get("creditor") or {}).get("name"),
            "bic": (payment.get("creditor") or {}).get("bic"),
            "bankCountry": (payment.get("creditor") or {}).get("bankCountry"),
        },
    }


def fetch_enrichment_proposals(
    payment_id: str, payment: dict, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> list[dict]:
    """POST the payment snapshot to the agent service; return its proposals (possibly []).

    Each proposal is `{field, to, reason, source}`. The caller validates `field` is in its
    allowlist before applying — this function does not filter, it just transports.
    """
    base = os.getenv("AGENTS_BACKEND_URL")
    if not base:
        # Agent not configured — demo runs deterministically without it.
        return []
    url = base.rstrip("/") + "/enrichment/propose"
    body = json.dumps(
        {"paymentId": payment_id, "payment": _snapshot_for_agent(payment)}
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - internal URL
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — never propagate to the saga.
        logger.warning(
            "enrichment-agent call failed for %s — returning no proposals",
            payment_id,
            exc_info=True,
        )
        return []
    proposals = payload.get("proposals") if isinstance(payload, dict) else None
    return proposals if isinstance(proposals, list) else []


class HttpEnrichmentAgent:
    """`EnrichmentAgent` port implementation over the separate payment-agent HTTP service.

    Constructed once by `PaymentsService` and injected into each `PaymentContext`. Returns
    `[]` when `AGENTS_BACKEND_URL` is unset, so dev environments without the agent service run
    deterministically. Implements the port from `ports.enrichment_agent` (imported lazily to
    keep the adapter↔port dependency one-directional at runtime; the port is the authority).
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    def propose(self, payment_id: str, payment: dict) -> list[dict]:
        return fetch_enrichment_proposals(
            payment_id, payment, timeout=self._timeout
        )
