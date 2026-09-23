"""Workflow UI routes — read-only reads over `payments` for the back-office surface.

Intentionally separate from the BIAN `/PaymentOrderInitiation/*` contract routes: these
serve the Payments Workflow UI only. Same split, and the same rationale, as the ledger
service's `routers/pipeline.py`.

All routes:  GET-only, prefix /workflow
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from routers._util import to_json_response
from services import workflow_read_service

router = APIRouter(prefix="/workflow", tags=["workflow"])


def _deps(request: Request):
    return request.app.state.connection, request.app.state.db_name


@router.get("/payments")
def list_payments(
    request: Request,
    status: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None, alias="customerId"),
    rail: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None, alias="from"),
    date_to: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(25, ge=1, le=100),
    skip: int = Query(0, ge=0),
) -> JSONResponse:
    connection, db_name = _deps(request)
    data = workflow_read_service.list_payments(
        connection, db_name,
        status=status, customer_id=customer_id, rail=rail,
        date_from=date_from, date_to=date_to, limit=limit, skip=skip,
    )
    return to_json_response(data)


@router.get("/exceptions")
def list_exceptions(
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    skip: int = Query(0, ge=0),
) -> JSONResponse:
    connection, db_name = _deps(request)
    data = workflow_read_service.list_exceptions(connection, db_name, limit=limit, skip=skip)
    return to_json_response(data)


@router.get("/stats")
def stats(
    request: Request,
    date_from: Optional[datetime] = Query(None, alias="from"),
    date_to: Optional[datetime] = Query(None, alias="to"),
) -> JSONResponse:
    connection, db_name = _deps(request)
    data = workflow_read_service.get_stats(connection, db_name, date_from=date_from, date_to=date_to)
    return to_json_response(data)


@router.get("/resolve/{ref}")
def resolve_ref(request: Request, ref: str) -> JSONResponse:
    """Resolve a PAY-/TXN-/ACC-/NOTIF- ref to its parent paymentId for the command search."""
    connection, db_name = _deps(request)
    result = workflow_read_service.resolve_ref(connection, db_name, ref)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no payment found for ref {ref}")
    return to_json_response(result)


# Declared last so the literal paths above are matched first — otherwise "/workflow/stats"
# would bind here as a paymentId.
@router.get("/payments/{payment_id}")
def get_payment(request: Request, payment_id: str) -> JSONResponse:
    connection, db_name = _deps(request)
    payment = workflow_read_service.get_payment(connection, db_name, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail=f"payment {payment_id} not found")
    return to_json_response(payment)
