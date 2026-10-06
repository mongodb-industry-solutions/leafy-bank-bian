"""HTTP clients for the transactions routes the Cutoff Agent's gated actions call.

The agent never writes `payments`. Hold, expedite and defer go through these routes so the
agent meets the same guards an operator does (untagged → 400; EXPEDITE past the external
cut-off or with an OPEN exception → refused). A 4xx surfaces as `clients.ServiceRefused`.
"""

from __future__ import annotations

import clients

EXPEDITE = "EXPEDITE"
NEXT_VALUE_DATE = "NEXT_VALUE_DATE"


def hold_next_value_date(payment_id: str, *, decided_by: str, reason: str) -> dict:
    return clients._post(f"{clients._transactions_base()}/PaymentOrderProcedure/HoldNextValueDate",
                         {"paymentId": payment_id, "decidedBy": decided_by, "reason": reason})


def cutoff_decision(payment_id: str, *, decision: str, decided_by: str) -> dict:
    return clients._post(f"{clients._transactions_base()}/PaymentOrderProcedure/CutoffDecision",
                         {"paymentId": payment_id, "decision": decision, "decidedBy": decided_by})
