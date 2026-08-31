import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from api_models import PaymentOrderBulkInitiateRequest, PaymentOrderInitiateRequest
from shared import party_authentication_token as party_auth
from database.connection import MongoDBConnection
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
