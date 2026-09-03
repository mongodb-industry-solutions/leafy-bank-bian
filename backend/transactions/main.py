import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from api_models import (
    FraudEvaluationRequest,
    PaymentConfirmationRequest,
    PaymentOrderBulkInitiateRequest,
    PaymentOrderInitiateRequest,
    PaymentSettlementInitiateRequest,
)
from shared import party_authentication_token as party_auth
from database.connection import MongoDBConnection
from contexts.payment_rail.domain import pacs008
from encoder.json_encoder import MyJSONEncoder
from routers.workflow import router as workflow_router
from services.payments_service import PaymentsService
from services.transactions_service import TransactionsService
from shared import registry

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")
# Stage-1 sanity bound on a malformed amount, not an entitlement limit — stage 2 owns
# the real per-payment decision, keyed on `customers.segment`
# (`contexts/party_authentication/domain/entitlement_policy.py`). Kept above every
# segment limit so it never vetoes a payment entitlement would have allowed.
PAYMENT_LIMIT_USD = float(os.getenv("PAYMENT_LIMIT_USD", "1000000"))

app = FastAPI(title="Leafy Bank — Payments (BIAN PaymentOrderInitiation)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

connection = MongoDBConnection(MONGODB_URI)
payments_service = PaymentsService(connection, DB_NAME, PAYMENT_LIMIT_USD)
transactions_service = TransactionsService(connection, DB_NAME)

# The /workflow routers resolve their dependencies from app.state rather than closing over
# the module globals, matching the ledger service's router convention.
app.state.connection = connection
app.state.db_name = DB_NAME

app.include_router(workflow_router)


def _bian_response(envelope: dict) -> Response:
    return Response(
        content=json.dumps(envelope, cls=MyJSONEncoder),
        media_type="application/json",
    )


def _strip(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


@app.get("/")
async def read_root():
    return {
        "service": "leafy-bank-payments",
        "bian": "PaymentOrderInitiation",
        "bianVersion": registry.bian_version,
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}


def _resolve_identity(body, authorization: Optional[str]) -> dict:
    """Who is this, per the token — or per the body if there is no token and none is required.

    Raises HTTP 401 rather than letting the AuthenticationError surface as a 500. Kept out
    of `_initiate_kwargs` so the single and bulk paths resolve identity once each, visibly,
    rather than inside a mapper that looks like pure field shuffling.
    """
    try:
        return party_auth.resolve_identity(authorization, body.customerId)
    except party_auth.AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))


def _initiate_kwargs(body) -> dict:
    """Map a validated request onto `initiate_payment`'s keyword arguments.

    Shared by Initiate and BulkInitiate so the two entry points cannot drift — a field
    added to the contract but wired into only one of them is the classic way a bulk path
    starts writing a different document shape than the single path.
    """
    creditor = body.creditor
    remittance = body.remittance
    return {
        "customer_ref": body.customerId,
        "debtor_account_ref": body.debtor.accountId,
        # None for an external beneficiary; the snapshot then rides on creditor_party.
        "creditor_account_ref": creditor.accountId,
        "creditor_party": creditor.model_dump(exclude={"accountId"}),
        "instructed_amount": body.instructedAmount,
        "instructed_currency": body.instructedCurrency,
        "payment_type": body.type,
        "payment_rail": body.rail,
        "remittance_unstructured": remittance.unstructured if remittance else None,
        "remittance_reference": remittance.reference if remittance else None,
        "remittance_invoice_no": remittance.invoiceNo if remittance else None,
        "priority": body.priority,
        "charge_bearer": body.chargeBearer,
        "category_purpose": body.categoryPurpose,
        "requested_execution_date": body.requestedExecutionDate,
        "channel": body.channel,
        # Stage 2 (doc 15 B1). The body's assertion is the pre-token fallback and is
        # OVERRIDDEN by `_resolve_identity` whenever a valid token is presented — a claim
        # the caller made about itself must never outrank one the bank signed. Left on the
        # contract until every caller sends a token (`REQUIRE_AUTHENTICATION`), then
        # removed. None here means nothing was asserted: stage 2 records `method: NONE`
        # and a SKIP, never a PASS.
        "authentication": body.authentication.model_dump() if body.authentication else None,
        "wire_details": body.wireDetails.model_dump() if body.wireDetails else None,
        "ach_details": body.achDetails.model_dump() if body.achDetails else None,
        "internal_details": body.internalDetails.model_dump() if body.internalDetails else None,
    }


@app.post("/PaymentOrderInitiation/Initiate")
async def payment_order_procedure_initiate(
    body: PaymentOrderInitiateRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
):
    identity = _resolve_identity(body, authorization)
    try:
        payment_doc = payments_service.initiate_payment(
            idempotency_key=idempotency_key or body.idempotencyKey,
            **{**_initiate_kwargs(body), **identity},
        )
        return _bian_response({
            "paymentId": payment_doc["paymentId"],
            "status": payment_doc["status"],
            "payment": _strip(payment_doc),
        })
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logging.error("PaymentOrderInitiation/Initiate failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal payment processing error.")


@app.post("/PaymentOrderInitiation/BulkInitiate")
async def payment_order_procedure_bulk_initiate(
    body: PaymentOrderBulkInitiateRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
):
    """Initiate a batch of payment orders sequentially. Each item reuses the single
    initiate path; per-item validation failures are captured in the response so a
    bad item can't abort the batch. Returns settled/failed counts plus per-item results."""
    results = []
    settled = 0
    for idx, item in enumerate(body.items):
        try:
            # Per item: a batch may legitimately name several customers, and an operator
            # token authorises that. A customer token does not, and each item is checked
            # against it — one bad item is a per-item error, not a failed batch.
            identity = party_auth.resolve_identity(authorization, item.customerId)
            payment_doc = payments_service.initiate_payment(
                idempotency_key=item.idempotencyKey,
                **{**_initiate_kwargs(item), **identity},
            )
            settled += 1
            results.append({
                "index": idx,
                "ok": True,
                "paymentId": payment_doc["paymentId"],
                "status": payment_doc["status"],
            })
        except party_auth.AuthenticationError as e:
            results.append({"index": idx, "ok": False, "error": str(e)})
        except ValueError as e:
            results.append({"index": idx, "ok": False, "error": str(e)})
        except Exception as e:
            logging.error("BulkInitiate item %d failed: %s", idx, e)
            results.append({"index": idx, "ok": False, "error": "Internal payment processing error."})

    return _bian_response({
        "requested": len(body.items),
        "settled": settled,
        "failed": len(body.items) - settled,
        "results": results,
    })


@app.get("/PaymentOrderInitiation/{paymentorderinitiationid}/Retrieve")
async def payment_order_initiation_retrieve(paymentorderinitiationid: str):
    try:
        payment = payments_service.retrieve_payment(paymentorderinitiationid)
        if not payment:
            raise HTTPException(status_code=404, detail="paymentId not found.")

        txn = payment.pop("_txn", None)
        return _bian_response({
            "paymentId": payment["paymentId"],
            "payment": _strip(payment),
            "transaction": _strip(txn) if txn else None,
        })
    except HTTPException:
        raise
    except Exception as e:
        logging.error("PaymentOrderInitiation/Retrieve failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal retrieve error.")


# --- Stage 4 — orchestration & authorization ---------------------------------
#
# ⚠️ **These routes do not run stage 4.** Orchestration and authorization execute inside the
# payment saga (`process/payment_lifecycle.py`), between validation and rail execution — a
# payment cannot be routed or authorised out of band, and re-running either against a settled
# payment would produce a second routing decision for a payment already in flight. So the
# two Retrieve operations read what the saga recorded, and the two Evaluate operations score
# **without persisting** — which is exactly what BIAN's `Evaluate` behaviour qualifier means,
# and what makes them useful to an operator asking "what would this score?".
#
# URL convention: `PaymentOrchestration` (48782) and `PaymentConfirmation` (47766) carry NO
# published semantic API in v14, so D8 governs their URLs. `FraudEvaluation` (44625) and
# `TransactionAuthorization` (43343) DO have published operations, and these match them.
#
# ⚠️ Doina's stage table names `PaymentAuthorization`. That SD does not exist in v14 (zero
# rows across the 341-SD landscape); `TransactionAuthorization` is the real name, and her own
# demo-display heading at L509 already reads "TRANSACTION AUTHORIZATION" (doc 18 B3, D7).

def _stage_four_artifacts(payment_id: str) -> dict:
    payment = payments_service.retrieve_payment(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="paymentId not found.")
    payment.pop("_txn", None)
    refs = payment.get("refs") or {}
    return {
        "payment": payment,
        "routingSnapshot": payments_service.routing_snapshots.find_one(
            {"routingSnapshotId": refs.get("routingSnapshotId")}
        ) if refs.get("routingSnapshotId") else None,
        "paymentOrder": payments_service.payment_orders.find_one(
            {"paymentOrderId": refs.get("paymentOrderId")}
        ) if refs.get("paymentOrderId") else None,
    }


@app.get("/PaymentOrchestration/{paymentorchestrationid}/Retrieve")
async def payment_orchestration_retrieve(paymentorchestrationid: str):
    """The routing decision as taken, plus the payment order it led to.

    `paymentorchestrationid` is the `paymentId` — orchestration has no identity of its own in
    this model; the routing snapshot and the payment order are its control records.
    """
    try:
        found = _stage_four_artifacts(paymentorchestrationid)
        payment = found["payment"]
        return _bian_response({
            "paymentId": payment["paymentId"],
            "state": (payment.get("lifecycle") or {}).get("currentState"),
            "clearingNetwork": (payment.get("wireDetails") or {}).get("network"),
            "routingSnapshot": _strip(found["routingSnapshot"]) if found["routingSnapshot"] else None,
            "paymentOrder": _strip(found["paymentOrder"]) if found["paymentOrder"] else None,
            "checks": [
                c for c in (payment.get("checks") or [])
                if str(c.get("stage", "")).startswith("4 orchestrate")
            ],
        })
    except HTTPException:
        raise
    except Exception as e:
        logging.error("PaymentOrchestration/Retrieve failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal retrieve error.")


@app.get("/TransactionAuthorization/{transactionauthorizationid}/Retrieve")
async def transaction_authorization_retrieve(transactionauthorizationid: str):
    """The authorization decision, the fraud assessment, and the screening result."""
    try:
        found = _stage_four_artifacts(transactionauthorizationid)
        payment = found["payment"]
        order = found["paymentOrder"] or {}
        return _bian_response({
            "paymentId": payment["paymentId"],
            "state": (payment.get("lifecycle") or {}).get("currentState"),
            "fraud": payment.get("fraud"),
            "sanctionsCheck": (payment.get("correspondent") or {}).get("sanctionsCheck"),
            "authorisedAt": (payment.get("clearing") or {}).get("authorisedAt"),
            "authorization": order.get("authorization"),
            "checks": [
                c for c in (payment.get("checks") or [])
                if str(c.get("stage", "")).startswith("4 authorize")
            ],
        })
    except HTTPException:
        raise
    except Exception as e:
        logging.error("TransactionAuthorization/Retrieve failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal retrieve error.")


# --- Stage 5: PaymentRail (SD 47741) ---------------------------------------
#
# ⚠️ These URLs are the **published v14 ones**, taken from the KG rather than invented:
# `kg PaymentRail -s bian` lists `GET /PaymentRail/{paymentrailid}/Retrieve`
# (`PaymentRailOperatingSession/Retrieve`) and
# `GET /PaymentRail/{paymentrailid}/OutboundTransaction/{outboundtransactionid}/Retrieve`.
# Doc 19 §3 step 7 planned a `/CanonicalJson/Retrieve` of our own; the real API has a proper
# behaviour qualifier for it, so the plan's version is dropped. Verifying an SD's URLs against
# the KG before writing them is the standing rule from defect 2026-07-06.
#
# `paymentrailid` is the `paymentId`: in this model the rail's operating session is scoped to
# the payment being executed, and `outboundtransactionid` is the `paymentExecutionId` — one
# outbound transaction per execution attempt, which is exactly her L854 grain.
#
# Both are GET retrieves. The saga owns the write path (stage 4's same deviation): a payment is
# executed by `POST /PaymentOrderInitiation/Initiate` running the lifecycle, never by calling
# the rail directly.

@app.get("/PaymentRail/{paymentrailid}/Retrieve")
async def payment_rail_retrieve(paymentrailid: str):
    """The rail operating session for one payment: every execution attempt, in order."""
    try:
        payment = payments_service.retrieve_payment(paymentrailid)
        if not payment:
            raise HTTPException(status_code=404, detail="paymentId not found.")
        executions = payments_service.list_payment_executions(paymentrailid)
        return _bian_response({
            "paymentId": payment["paymentId"],
            "state": (payment.get("lifecycle") or {}).get("currentState"),
            "rail": payment.get("rail"),
            "clearingNetwork": (payment.get("wireDetails") or {}).get("network"),
            "clearing": payment.get("clearing"),
            "attempts": [_strip(e) for e in executions],
            "checks": [
                c for c in (payment.get("checks") or [])
                if str(c.get("stage", "")).startswith("5 execute")
            ],
        })
    except HTTPException:
        raise
    except Exception as e:
        logging.error("PaymentRail/Retrieve failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal retrieve error.")


@app.get("/PaymentRail/{paymentrailid}/OutboundTransaction/{outboundtransactionid}/Retrieve")
async def payment_rail_outbound_transaction_retrieve(
    paymentrailid: str, outboundtransactionid: str
):
    """One execution attempt: the pacs.008 as sent, plus the stored message record.

    This is what the demo's ISO VIEW reads — the business half comes from
    `paymentMessages.payload`, the ISO half from `paymentExecutions.message`.
    """
    try:
        execution = payments_service.get_payment_execution(outboundtransactionid)
        if not execution or execution.get("paymentId") != paymentrailid:
            raise HTTPException(status_code=404, detail="paymentExecutionId not found.")
        message = payments_service.get_payment_message(execution.get("paymentMessageId"))
        return _bian_response({
            "paymentId": execution["paymentId"],
            "paymentExecutionId": execution["paymentExecutionId"],
            "attempt": execution.get("attempt"),
            "messageStandard": execution.get("messageStandard"),
            "messageFormat": execution.get("messageFormat"),
            "message": execution.get("message"),
            # Derived, not stored — see `workflow_read_service._with_xml`.
            "messageXml": (
                pacs008.to_xml(execution["message"]) if execution.get("message") else None
            ),
            "status": execution.get("status"),
            "railStatus": execution.get("railStatus"),
            "simulated": execution.get("simulated"),
            "canonicalPayload": (message or {}).get("payload"),
            "mappingVersion": (message or {}).get("mappingVersion"),
            "transformationAudit": (message or {}).get("transformationAudit"),
        })
    except HTTPException:
        raise
    except Exception as e:
        logging.error("PaymentRail/OutboundTransaction/Retrieve failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal retrieve error.")


@app.post("/FraudEvaluation/Evaluate")
async def fraud_evaluation_evaluate(body: FraudEvaluationRequest):
    """Score a payment's rules WITHOUT persisting anything.

    Reads the stored payment and re-runs the rule set over it, so an operator can see which
    rules fired and why. Non-mutating on purpose: the score that counts is the one stage 4b
    wrote at the AUTHORISED transition, and a second, later score would invite the question
    of which one authorised the payment.
    """
    try:
        result = payments_service.evaluate_fraud(body.paymentId)
        if result is None:
            raise HTTPException(status_code=404, detail="paymentId not found.")
        return _bian_response(result)
    except HTTPException:
        raise
    except Exception as e:
        logging.error("FraudEvaluation/Evaluate failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal evaluation error.")


@app.post("/PaymentConfirmation/Execute")
async def payment_confirmation_execute(body: PaymentConfirmationRequest):
    """Re-send the originator confirmation for a payment that has an execution path.

    Her L502 places this *"once orchestration has committed to an execution path, independent
    of the final settlement confirmation much later."* Stage 4b records it automatically; this
    operation exists for an operator re-sending it, and refuses when there is nothing to
    confirm — a confirmation for an uncommitted payment would be a false statement to the
    customer.
    """
    try:
        result = payments_service.confirm_to_originator(body.paymentId)
        if result is None:
            raise HTTPException(status_code=404, detail="paymentId not found.")
        return _bian_response(result)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logging.error("PaymentConfirmation/Execute failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal confirmation error.")


@app.post("/PaymentSettlement/Initiate")
async def payment_settlement_initiate(body: PaymentSettlementInitiateRequest):
    """BIAN `POST /PaymentSettlement/Initiate` (doc 21 B6 — SD 40033, no published API).

    Triggers or re-triggers settlement for one payment at IN_PROGRESS. The `outcome`
    field drives the simulated settlement response (B4): matched (default), delayed,
    unmatched, or exception. This is the operator-facing endpoint for demo scenarios
    that need a specific settlement outcome — the saga's own default is matched.
    """
    try:
        result = payments_service.settle_payment(body.paymentId, body.outcome)
        if result is None:
            raise HTTPException(status_code=404, detail="paymentId not found.")
        return _bian_response(result)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logging.error("PaymentSettlement/Initiate failed: %s", e)
        raise HTTPException(status_code=500, detail="Internal settlement error.")
