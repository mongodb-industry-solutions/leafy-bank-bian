import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from api_models import PaymentOrderInitiateRequest
from database.connection import MongoDBConnection
from encoder.json_encoder import MyJSONEncoder
from services.payments_service import PaymentsService
from services.transactions_service import TransactionsService
from shared import registry

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")
PAYMENT_LIMIT_USD = float(os.getenv("PAYMENT_LIMIT_USD", "500"))

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


@app.post("/PaymentOrderInitiation/Initiate")
async def payment_order_procedure_initiate(
    body: PaymentOrderInitiateRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    try:
        payment_doc = payments_service.initiate_payment(
            customer_ref=body.customerId,
            debtor_account_ref=body.debtor.accountId,
            creditor_account_ref=body.creditor.accountId,
            instructed_amount=body.instructedAmount,
            instructed_currency=body.instructedCurrency,
            payment_type=body.type,
            payment_rail=body.rail,
            remittance_unstructured=(body.remittance.unstructured if body.remittance else None),
            idempotency_key=idempotency_key,
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
