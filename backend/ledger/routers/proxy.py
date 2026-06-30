"""Proxy routes — forwards browser-originated calls to internal cluster services.

The GL Monitor page is served from the ledger backend. Browsers can reach the
ledger externally, but the transactions service runs on internal Kanopy DNS
(leafy-bank-bian-transactions-web-app) which is unreachable from outside the
cluster. These routes let the browser call the ledger; the server forwards.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["proxy"])

_TRANSACTIONS_BASE_DEFAULT = "http://leafy-bank-bian-transactions-web-app:80"


@router.post("/PaymentOrderInitiation/Initiate")
async def proxy_initiate_payment(request: Request) -> JSONResponse:
    txn_base = os.getenv("TRANSACTIONS_BASE_URL", _TRANSACTIONS_BASE_DEFAULT).rstrip("/")
    body = await request.body()
    url = f"{txn_base}/PaymentOrderInitiation/Initiate"
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return JSONResponse(content=json.loads(resp.read()), status_code=resp.status)
    except urllib.error.HTTPError as e:
        return JSONResponse(content=json.loads(e.read()), status_code=e.code)
