"""HTTP clients for the service routes the Reconciliation Agent acts through.

The agent reads the shared DB directly, but every action that changes an exception's status
or moves money goes through the owning service's route, so the agent gets exactly the
guards an operator gets (`_LEGAL`, the chargeBearer gate, the OPEN claim). Stdlib `urllib`,
the same pattern as `ledger/routers/proxy.py` — no new dependency.

A 4xx is the service refusing the action (422 not legal, 409 conflict, 404 gone). It is
raised as `ServiceRefused` so the graph can record the refusal and re-plan, rather than
treating a guard doing its job as a crash.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

_TIMEOUT_SECONDS = 30


class ServiceRefused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def _ledger_base() -> str:
    return os.getenv("LEDGER_BASE_URL", "http://localhost:8003").rstrip("/")


def _transactions_base() -> str:
    return os.getenv("TRANSACTIONS_BASE_URL", "http://localhost:8002").rstrip("/")


def _post(url: str, body: Optional[dict] = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            try:
                detail = json.loads(e.read() or b"{}").get("detail", "")
            except ValueError:
                detail = ""
            raise ServiceRefused(e.code, str(detail)) from None
        raise
    return json.loads(raw) if raw else {}


def recheck(exception_id: str, note: Optional[str] = None) -> dict:
    return _post(f"{_ledger_base()}/pipeline/exceptions/{exception_id}/recheck", {"note": note})


def link(exception_id: str, *, payment_id: Optional[str] = None,
         payment_message_id: Optional[str] = None, line_no: Optional[int] = None,
         note: Optional[str] = None) -> dict:
    return _post(f"{_ledger_base()}/pipeline/exceptions/{exception_id}/link",
                 {"paymentId": payment_id, "paymentMessageId": payment_message_id,
                  "lineNo": line_no, "note": note})


def reconcile(payment_id: str) -> dict:
    return _post(f"{_ledger_base()}/pipeline/reconcile/{payment_id}")


def resolve(exception_id: str, action: str, note: Optional[str] = None) -> dict:
    return _post(f"{_transactions_base()}/workflow/exceptions/{exception_id}/resolve",
                 {"action": action, "note": note})
