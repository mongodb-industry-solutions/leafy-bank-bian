"""Pipeline UI routes — read-only monitoring for the GL Pipeline Monitor dashboard.

These routes are intentionally separate from routers/financial_accounting.py.
They serve the UI only and are not part of the BIAN FinancialAccounting contract.

Routes are GET-only, prefix /pipeline, except the triggers (batch, statement matching,
per-payment reconcile), which write.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from routers._util import to_json_response
from pydantic import BaseModel, ConfigDict

from services import correspondent_reply, pipeline_read_service, reconciliation_service, resolution_service, statement_matching
from workers import gl_batch

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.get("/health")
def pipeline_health(request: Request) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name
    interval = int(os.getenv("GL_BATCH_INTERVAL_SECONDS", "600"))
    next_batch_at = getattr(request.app.state, "batch_status", {}).get("nextRunAt")
    data = pipeline_read_service.get_pipeline_health(
        connection, db_name, batch_interval_seconds=interval, next_batch_at=next_batch_at
    )
    return to_json_response(data)


@router.get("/gl-dashboard")
def gl_dashboard(
    request: Request,
    period_code: Optional[str] = Query(None, alias="periodCode"),
    months: int = Query(3, ge=1, le=24),
) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name
    data = pipeline_read_service.get_gl_dashboard(
        connection, db_name, period_code=period_code, months=months
    )
    return to_json_response(data)


@router.get("/transactions")
def transactions_feed(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    period_code: Optional[str] = Query(None, alias="periodCode"),
) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name
    data = pipeline_read_service.list_transactions(
        connection, db_name, limit=limit, period_code=period_code
    )
    return to_json_response(data)


@router.get("/ledger-events")
def ledger_events_feed(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    period_code: Optional[str] = Query(None, alias="periodCode"),
) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name
    data = pipeline_read_service.list_ledger_events(
        connection, db_name, limit=limit, status=status, period_code=period_code
    )
    return to_json_response(data)


@router.get("/subledger-entries")
def subledger_entries_feed(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
    period_code: Optional[str] = Query(None, alias="periodCode"),
    control_account_code: Optional[str] = Query(None, alias="controlAccountCode"),
) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name
    data = pipeline_read_service.list_subledger_entries(
        connection,
        db_name,
        limit=limit,
        period_code=period_code,
        control_account_code=control_account_code,
    )
    return to_json_response(data)


@router.get("/journals")
def journals_feed(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    period_code: Optional[str] = Query(None, alias="periodCode"),
) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name
    data = pipeline_read_service.list_journal_entries(
        connection, db_name, limit=limit, period_code=period_code
    )
    return to_json_response(data)


@router.get("/trace/{payment_id}")
def trace_payment(payment_id: str, request: Request) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name
    result = pipeline_read_service.trace_payment(payment_id, connection, db_name)
    if result is None:
        raise HTTPException(status_code=404, detail=f"payment {payment_id} not found")
    return to_json_response(result)


@router.post("/batch/trigger")
def trigger_batch(request: Request) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name
    coa = request.app.state.coa
    result = gl_batch.run_one_cycle(connection, db_name, coa)
    return to_json_response(result)


@router.post("/statements/match")
def match_statements(request: Request) -> JSONResponse:
    """Reconciliation plan A2 — match correspondent statement lines to settlement positions."""
    connection, db_name = request.app.state.connection, request.app.state.db_name
    result = statement_matching.match_statements(connection, db_name)
    result.update(statement_matching.raise_orphans(connection, db_name))
    return to_json_response(result)


@router.post("/reconcile/{payment_id}")
def reconcile_payment(
    payment_id: str,
    request: Request,
    match: bool = Query(False, description="Run statement matching + orphan raise first"),
) -> JSONResponse:
    """Plan A3 — re-run the three-way reconciliation for one payment (the spec's
    "re-run reconciliation" step; A4's RECHECK calls it with `match=true`)."""
    connection, db_name = request.app.state.connection, request.app.state.db_name
    payment = connection.get_collection(db_name, "payments").find_one(
        {"paymentId": payment_id}, {"_id": 0, "lifecycle": 1})
    if payment is None:
        raise HTTPException(status_code=404, detail=f"payment {payment_id} not found")
    if not reconciliation_service.is_eligible(payment):
        lifecycle = payment.get("lifecycle") or {}
        raise HTTPException(status_code=409, detail=(
            f"payment {payment_id} is not reconcilable: currentState="
            f"{lifecycle.get('currentState')}, reconciliationStatus={lifecycle.get('reconciliationStatus')}"))
    if match:
        statement_matching.match_statements(connection, db_name)
        statement_matching.raise_orphans(connection, db_name)
    check, outcome = reconciliation_service.reconcile_payment(payment_id, connection, db_name)
    return to_json_response({"outcome": outcome, "check": check.as_dict()})


class _ActionBody(BaseModel):
    note: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class _LinkBody(_ActionBody):
    # ORPHANED_SETTLEMENT: the payment the line belongs to. MISSING: the line.
    paymentId: Optional[str] = None
    paymentMessageId: Optional[str] = None
    lineNo: Optional[int] = None


def _resolution_call(fn, *args, **kwargs) -> JSONResponse:
    try:
        return to_json_response(fn(*args, **kwargs))
    except resolution_service.NotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except resolution_service.Conflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except resolution_service.NotLegal as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/exceptions/{exception_id}/recheck")
def recheck_exception(exception_id: str, request: Request,
                      body: Optional[_ActionBody] = None) -> JSONResponse:
    """Plan A4 RECHECK — match statements, re-run the tie-out, resolve only on RECONCILED."""
    connection, db_name = request.app.state.connection, request.app.state.db_name
    return _resolution_call(resolution_service.recheck, connection, db_name, exception_id,
                            note=body.note if body else None)


@router.post("/exceptions/{exception_id}/correspondent-reply")
def correspondent_reply_exception(exception_id: str, request: Request) -> JSONResponse:
    """Demo: the correspondent answers an escalated exception now, without the 20s wait."""
    connection, db_name = request.app.state.connection, request.app.state.db_name
    return _resolution_call(correspondent_reply.reply, connection, db_name, exception_id)


@router.post("/exceptions/{exception_id}/link")
def link_exception(exception_id: str, body: _LinkBody, request: Request) -> JSONResponse:
    """Plan A4 LINK_STATEMENT_ENTRY — pair a statement line with a payment; closes both twins."""
    connection, db_name = request.app.state.connection, request.app.state.db_name
    return _resolution_call(resolution_service.link, connection, db_name, exception_id,
                            payment_id=body.paymentId, payment_message_id=body.paymentMessageId,
                            line_no=body.lineNo, note=body.note)


@router.get("/reconciliation")
def reconciliation(
    request: Request,
    period_code: Optional[str] = Query(None, alias="periodCode"),
) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name
    results = reconciliation_service.reconcile_all_accounts(
        connection, db_name, period_code=period_code
    )
    payload = [
        {
            "accountCode": r.account_code,
            "periodCode": r.period_code,
            "subledgerSum": r.subledger_sum,
            "journalSum": r.journal_sum,
            "isReconciled": r.is_reconciled,
            "checkedAt": r.checked_at.isoformat(),
        }
        for r in results
    ]
    return to_json_response(payload)
