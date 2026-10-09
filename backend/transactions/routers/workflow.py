"""Workflow UI routes over `payments` for the back-office surface.

Intentionally separate from the BIAN `/PaymentOrderInitiation/*` contract routes: these
serve the Payments Workflow UI only. Same split, and the same rationale, as the ledger
service's `routers/pipeline.py`.

Routes:  GET-only reads, prefix /workflow — PLUS the two `/demo/*` simulator triggers (plan B3) and the operational write route
`POST /workflow/exceptions/{exceptionId}/resolve` (doc 24 B6). No BIAN service domain for
exceptions (row 9: modeled within the originating domain), so the resolve endpoint rides the
ops namespace, sanctioned by the ledger's `POST /pipeline/batch/trigger` precedent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from api_models import (
    CutoffReleaseRequest,
    CutoffScenarioRequest,
    DemoClockRequest,
    ExceptionResolveRequest,
    ReconScenarioRequest,
    UtaResolveRequest,
)
from process.exceptions import ExceptionActionNotLegal, ExceptionConflict, ExceptionNotFound
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
    direction: Optional[Literal["INBOUND", "OUTBOUND"]] = Query(None),
    corridor: Optional[Literal["DOMESTIC", "INTERNATIONAL"]] = Query(None),
    date_from: Optional[datetime] = Query(None, alias="from"),
    date_to: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(25, ge=1, le=100),
    skip: int = Query(0, ge=0),
) -> JSONResponse:
    connection, db_name = _deps(request)
    data = workflow_read_service.list_payments(
        connection, db_name,
        status=status, customer_id=customer_id, rail=rail, direction=direction,
        corridor=corridor, date_from=date_from, date_to=date_to, limit=limit, skip=skip,
    )
    return to_json_response(data)


@router.get("/exceptions")
def list_exceptions(
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    skip: int = Query(0, ge=0),
    status: Optional[str] = Query("OPEN", description="OPEN | RESOLVED | DISMISSED | ALL"),
    category: Optional[str] = Query(None),
) -> JSONResponse:
    connection, db_name = _deps(request)
    data = workflow_read_service.list_exceptions(
        connection, db_name, limit=limit, skip=skip,
        status=None if (status or "").upper() == "ALL" else status,
        category=category,
    )
    return to_json_response(data)


# The one operational write route under /workflow (doc 24 B6). The guard chain (exception
# exists → OPEN → action legal for category) lives in `payments_service.resolve_exception`,
# which raises ValueError with a distinguishable message; the router maps those to the
# 404 / 409 / 422 the plan's step-5 gate requires.
@router.post("/exceptions/{exception_id}/resolve")
def resolve_exception(
    request: Request,
    exception_id: str,
    body: ExceptionResolveRequest,
) -> JSONResponse:
    svc = request.app.state.payments_service
    try:
        updated = svc.resolve_exception(
            exception_id,
            action=body.action,
            note=body.note,
            new_settlement_outcome=body.newSettlementOutcome,
        )
        return to_json_response(updated)
    # Typed errors first — the status no longer depends on message wording. The string
    # fallback below still covers untyped ValueErrors from deeper modules.
    except ExceptionNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ExceptionConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ExceptionActionNotLegal as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        msg = str(e)
        if "not found" in msg:
            code = 404
        # 409 = the resolve conflicts with current state: the exception is already closed,
        # another resolver won the race, or the payment has moved past the state the action
        # needs (a RETRY on a payment no longer IN_PROGRESS — which the service now refuses
        # BEFORE writing, so the exception stays open and retryable).
        elif "not OPEN" in msg or "not IN_PROGRESS" in msg or ("is " in msg and "OPEN" in msg):
            code = 409
        elif "not legal" in msg:
            code = 422
        else:
            code = 400
        raise HTTPException(status_code=code, detail=msg)
    except Exception as e:
        import logging
        logging.error("resolve_exception failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal exception resolution error.")


# The inbound queue's own write route (FR-9.IN2). Separate from `/resolve` because UTA's
# two actions are structurally different from the outbound ones — Repair takes an account
# and resumes the saga, Return generates a pacs.004 and closes the payment. Folding them
# into the generic resolver would make every field optional for every action.
@router.post("/exceptions/{exception_id}/uta")
def resolve_uta(
    request: Request,
    exception_id: str,
    body: UtaResolveRequest,
) -> JSONResponse:
    svc = request.app.state.payments_service
    try:
        updated = svc.resolve_uta(
            exception_id,
            action=body.action,
            matched_account_id=body.matchedAccountId,
            return_reason_code=body.returnReasonCode,
            note=body.note,
        )
        return to_json_response(updated)
    except ValueError as e:
        msg = str(e)
        if "not found" in msg:
            code = 404
        elif "not OPEN" in msg or "stays open" in msg:
            # The payment moved past the state the action needs, or another resolver won —
            # the exception is untouched and the action can be retried.
            code = 409
        elif "not legal" in msg or "requires" in msg:
            code = 422
        else:
            code = 400
        raise HTTPException(status_code=code, detail=msg)
    except Exception as e:
        import logging
        logging.error("resolve_uta failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal exception resolution error.")


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


# Plan B3 — demo controls for the reconciliation agent scenarios. Simulator routes, like
# `/FinancialGateway/{id}/Statement/Generate`: no BIAN operation, ops namespace.
@router.post("/demo/recon-scenarios")
def load_recon_scenarios(request: Request) -> JSONResponse:
    """Initiate R1–R4 through the real saga, settle them, book one statement with the R5
    orphan. Then trigger the ledger's GL batch to raise the exceptions."""
    from contexts.financial_gateway.application import recon_scenarios

    connection, db_name = _deps(request)
    try:
        return to_json_response(recon_scenarios.run(request.app.state.payments_service,
                                                    connection, db_name))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/demo/advance-cycle")
def advance_statement_cycle(request: Request) -> JSONResponse:
    """Settle what is due and book the next statement (R3's late line lands here)."""
    from contexts.financial_gateway.application import recon_scenarios

    connection, db_name = _deps(request)
    doc = recon_scenarios.advance_cycle(request.app.state.payments_service, connection, db_name)
    return to_json_response({"generated": doc is not None,
                             "paymentMessageId": doc and doc["paymentMessageId"],
                             "entryCount": len(doc["entries"]) if doc else 0})


# Plan E — the scenario walkthrough: one wire per click, then settle on demand.
@router.post("/demo/recon-scenario")
def initiate_recon_scenario(body: ReconScenarioRequest, request: Request) -> JSONResponse:
    """Initiate one walkthrough scenario's wire. No settle, no statement."""
    from contexts.financial_gateway.application import recon_scenarios

    if body.scenario not in recon_scenarios.WALKTHROUGH_SCENARIOS:
        raise HTTPException(status_code=422, detail=f"unknown scenario {body.scenario!r}")
    try:
        return to_json_response(recon_scenarios.run_one(request.app.state.payments_service,
                                                        body.scenario))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Cutoff plan Part B — one hold per click, on a fresh demo clock run.
@router.post("/demo/cutoff-scenario")
def initiate_cutoff_scenario(body: CutoffScenarioRequest, request: Request) -> JSONResponse:
    """Start one cutoff scenario (C1–C5) through the real saga; it stops at its hold."""
    from contexts.payment_orchestration.application import cutoff_scenarios

    try:
        return to_json_response(cutoff_scenarios.run_one(request.app.state.payments_service,
                                                         body.scenario))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/demo/clock")
def move_demo_clock(body: DemoClockRequest, request: Request) -> JSONResponse:
    """Move one demo clock run: anchor / advance forward, or reset to its start minute."""
    from contexts.payment_orchestration.application import cutoff_scenarios

    try:
        return to_json_response(cutoff_scenarios.move_clock(
            request.app.state.payments_service.db, body.runId, anchor=body.anchor,
            advance_minutes=body.advanceMinutes, reset=bool(body.reset),
        ))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/demo/cutoff-release")
def release_cutoff_run(body: CutoffReleaseRequest, request: Request) -> JSONResponse:
    """Fast-forward one run to the next business day and close its held payments."""
    from contexts.payment_orchestration.application import cutoff_release

    try:
        return to_json_response(cutoff_release.release_run(
            request.app.state.payments_service, body.runId, actor=cutoff_release.ROUTE_ACTOR))
    except cutoff_release.ReleaseBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/demo/settle-due")
def settle_due(request: Request) -> JSONResponse:
    """Settle every captured wire now (the settlement worker's job, without its 30s wait)."""
    from contexts.payment_settlement import settle

    connection, db_name = _deps(request)
    return to_json_response({"settled": settle.complete_due(connection, db_name, delay_seconds=0)})
