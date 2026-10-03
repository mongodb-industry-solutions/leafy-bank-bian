"""A simulated wire gateway. Deterministic, offline, and labelled as simulated.

*"The demo will not connect to a real payment network"* — so this adapter is the deliverable,
not a placeholder for one. It performs no I/O, sleeps for nothing, and derives every value in
its acknowledgement from the message it was handed, which is what makes
`test_the_simulated_rail_is_deterministic` possible.

⚠️ **Every document this produces is stamped `simulated: True`.** The routing snapshot's
correspondent block set that precedent for exactly this reason: an audience must be able to
see which parts of the demo are real MongoDB behaviour and which are stand-ins.

The status codes are ISO 20022 `pacs.002` external status codes — `ACSP` (accepted, settlement
in process) and `RJCT` (rejected) — because that is the message a real rail would answer a
pacs.008 with (her L221). We do not build the pacs.002 document itself; that is stage 9's
returns/status work (doc 19 §6).
"""

from __future__ import annotations

from typing import Optional

from contexts.payment_rail.domain import pacs008
from contexts.payment_rail.ports.rail import RailAck

# `pacs.002` ExternalPaymentTransactionStatus codes.
ACCEPTED = "ACSP"
REJECTED = "RJCT"

# Per-network reference prefixes. Fedwire calls it an IMAD, CHIPS a system sequence number,
# SWIFT a transaction reference — one dict rather than three branches, because the only thing
# that varies is the label.
_NETWORK_PREFIX = {
    "FEDWIRE": "IMAD",
    "CHIPS": "SSN",
    "SWIFT": "SWIFTREF",
    "LYNX": "LYNXREF",
    "SEPA": "SEPAREF",
}

# ISO 20022 `LocalInstrument` / network codes as the demo reports them on
# `payments.clearing.networkCode`. The internal book-transfer value ("0000") is the spec's
# own, from the `internal_transfer` sample — it is stamped in `execute.py`, not here, because
# a book transfer never reaches a gateway.
_NETWORK_CODE = {
    "FEDWIRE": "FW",
    "CHIPS": "CH",
    "SWIFT": "SW",
    "LYNX": "LX",
    "SEPA": "SP",
}


class SimulatedWireRail:
    """Accepts any well-formed pacs.008 and answers `ACSP`.

    "Well-formed" is deliberately shallow — the message must name an amount and a creditor
    agent. Deep ISO validation belongs to a real gateway; simulating a rejection the demo has
    no story for would only produce failures nobody can explain.
    """

    def submit(self, message: dict, *, network: Optional[str],
               payment_id: str) -> RailAck:
        # Through the envelope: a pacs.008 is `Document/FIToFICstmrCdtTrf/…` and
        # `CdtTrfTxInf` is 1..n. `pacs008.body` is the one place that knows the wrapper, so a
        # gateway reading the message flat cannot silently reject every well-formed one —
        # which is exactly what happened when the envelope landed and this was not updated.
        body = pacs008.body(message)
        group_header = body.get("GrpHdr") or {}
        transactions = body.get("CdtTrfTxInf") or []
        txn = transactions[0] if transactions else {}
        # `#text` — the amount is the element's text node, `Ccy` is an attribute. Read via
        # the module's own constant so a convention change cannot leave this reading a key
        # that no longer exists (it already did once: see defects.md 2026-09-02).
        amount = (txn.get("IntrBkSttlmAmt") or {}).get(pacs008.TEXT_KEY)
        creditor_agent = txn.get("CdtrAgt")

        if not amount or not creditor_agent:
            return RailAck(
                accepted=False,
                status_code=REJECTED,
                reason=(
                    "Message rejected at the gateway: a pacs.008 requires an interbank "
                    "settlement amount and a creditor agent."
                ),
                message_ref=self._message_ref(network, payment_id),
            )

        return RailAck(
            accepted=True,
            network_ref=self._network_ref(network, payment_id),
            network_code=_NETWORK_CODE.get((network or "").upper()),
            status_code=ACCEPTED,
            reason="Accepted for settlement (SIMULATED).",
            message_ref=self._message_ref(network, payment_id),
            # From the GROUP header: that is where the format specification puts it, and a
            # real rail echoes back the settlement date it was instructed with.
            settlement_date=group_header.get("IntrBkSttlmDt"),
        )

    @staticmethod
    def _network_ref(network: Optional[str], payment_id: str) -> str:
        """The rail's own reference for the payment. Derived from `paymentId` so it is stable
        across a replay — a simulated rail that invented a random reference would make the
        demo's reconciliation story unreproducible."""
        prefix = _NETWORK_PREFIX.get((network or "").upper(), "RAILREF")
        return f"{prefix}-{payment_id.split('-', 1)[-1]}"

    @staticmethod
    def _message_ref(network: Optional[str], payment_id: str) -> str:
        return f"PACS002-{payment_id.split('-', 1)[-1]}"
