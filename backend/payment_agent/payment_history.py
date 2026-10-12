"""Readers for the lifecycle `EVENT` documents in `paymentHistory`.

`payments.lifecycle.events[]` moved out of the payment document (the transactions service owns
the design: `lifecycle_history.py`). The agent only needs the latest event and the SUBMITTED
event, so it reads those two shapes directly rather than importing across services.
"""

from __future__ import annotations

from typing import Any, Optional

HISTORY = "paymentHistory"
EVENT = "EVENT"
CHECK = "CHECK"


def latest_event(db: Any, payment_id: Optional[str]) -> Optional[dict]:
    rows = list(db[HISTORY].find({"paymentId": payment_id, "kind": EVENT}).sort("seq", -1).limit(1))
    return rows[0] if rows else None


def latest_event_with_state(db: Any, payment_id: Optional[str], state: str) -> Optional[dict]:
    rows = list(db[HISTORY].find(
        {"paymentId": payment_id, "kind": EVENT, "state": state}).sort("seq", -1).limit(1))
    return rows[0] if rows else None


def history_for(db: Any, payment_id: Optional[str]) -> tuple[list[dict], list[dict]]:
    """`(events, checks)` for a payment, oldest first, as the embedded arrays once held them."""
    rows = list(db[HISTORY].find({"paymentId": payment_id}, {"_id": 0}).sort("seq", 1))
    return ([r for r in rows if r.get("kind") == EVENT],
            [r for r in rows if r.get("kind") == CHECK])
