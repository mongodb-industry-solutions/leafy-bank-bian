"""Pipeline UI routes — read-only monitoring for the GL Pipeline Monitor dashboard.

These routes are intentionally separate from routers/financial_accounting.py.
They serve the UI only and are not part of the BIAN FinancialAccounting contract.

All routes:  GET-only, prefix /pipeline
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from routers._util import to_json_response
from services import pipeline_read_service, reconciliation_service
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
    top_n: int = Query(5, ge=1, le=50, alias="topN"),
) -> JSONResponse:
    connection = request.app.state.connection
    db_name = request.app.state.db_name
    data = pipeline_read_service.get_gl_dashboard(
        connection, db_name, period_code=period_code, months=months, top_n=top_n
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
