"""BIAN FinancialAccounting read routes.

Routes:
  GET /FinancialAccounting/{financialaccountingid}/Retrieve
  GET /FinancialAccounting/{financialaccountingid}/LedgerPosting/{ledgerpostingid}/Retrieve
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from routers._util import to_json_response
from services import gl_read_service

router = APIRouter()


@router.get("/FinancialAccounting/{financialaccountingid}/Retrieve")
def retrieve_financial_accounting(
    financialaccountingid: str,
    request: Request,
    period_code: Optional[str] = Query(None, alias="periodCode"),
) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name

    gl_account = gl_read_service.get_gl_account(financialaccountingid, connection, db_name)
    if gl_account is None:
        raise HTTPException(status_code=404, detail=f"GL account {financialaccountingid} not found")

    journals = gl_read_service.get_journal_entries_for_account(
        financialaccountingid, connection, db_name, period_code=period_code,
    )

    return to_json_response({
        "financialAccountingId": financialaccountingid,
        "glAccount": gl_account,
        "recentJournals": journals,
    })


@router.get("/FinancialAccounting/{financialaccountingid}/LedgerPosting/{ledgerpostingid}/Retrieve")
def retrieve_ledger_posting(
    financialaccountingid: str,
    ledgerpostingid: str,
    request: Request,
) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name

    event = gl_read_service.get_ledger_event(ledgerpostingid, connection, db_name)
    if event is None:
        raise HTTPException(status_code=404, detail=f"LedgerPosting {ledgerpostingid} not found")

    return to_json_response(event)


# --- Stage 8: AccountReconciliation (BIAN SD 35449) ---------------------------
# One route, exposing the three-way reconciliation result for one payment (doc 22 B6). The UI
# reads this from /pipeline/trace/{id}'s `reconciliation` block; this route is the canonical BIAN
# surface for conformance and curl. Same service function, two thin routes.

from pydantic import BaseModel

from services import reconciliation_service as _recon_service


class AccountReconciliationRetrieveRequest(BaseModel):
    paymentId: str


@router.post("/AccountReconciliation/Retrieve")
def retrieve_account_reconciliation(
    body: AccountReconciliationRetrieveRequest,
    request: Request,
) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name

    check = _recon_service.compute_reconciliation(body.paymentId, connection, db_name)
    if check is None:
        raise HTTPException(status_code=404, detail=f"Payment {body.paymentId} not found")

    return to_json_response(check.as_dict())
