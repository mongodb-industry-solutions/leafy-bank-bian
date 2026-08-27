"""Builds the canonical `payments` document. Pure — no I/O, no clock.

BIAN PaymentOrderInitiation (SD 42933). The envelope shape is
`doinas-research/propose_payments.json`; field-level gaps against pain.001 are catalogued
in `07-schema-coverage-gaps.md`.

Pure by design: `persist.py` does the insert, this decides the shape. That split is what
lets the document shape be tested without a database, which matters because the envelope
migration (deferred, plan §9) will rewrite most of this.
"""

from __future__ import annotations

from typing import Optional

from contexts.payment_order_initiation.domain import lifecycle
from shared.refs import derive_ref

# v6: payments.debtor.accountType / creditor.accountType share the same enum (title-case).
_ACCOUNT_TYPE_DISPLAY = {
    "CURRENT": "Current",
    "SAVINGS": "Savings",
    "CHECKING": "Checking",
    "FIXED_DEPOSIT": "FixedDeposit",
}


def display_account_type(account_type: Optional[str]) -> Optional[str]:
    if not account_type:
        return None
    return _ACCOUNT_TYPE_DISPLAY.get(account_type)


def party_snapshot(customer: dict, account: dict) -> dict:
    """Debtor / creditor agent block, denormalised onto the payment at initiation time."""
    identification = customer.get("identification", {}) or {}
    return {
        "accountId": account["accountId"],
        "accountNo": account.get("accountNumber"),
        "iban": account.get("iban"),
        "name": identification.get("legalName"),
        "bic": "LEAFUS33",
        "address": (customer.get("contact", {}) or {}).get("addresses", []),
        "accountType": display_account_type(account.get("type")),
        # TODO (D10, stage 3 enrichment): clearingSystemMemberId + clearingSystemCode.
    }


def build(ctx) -> dict:
    """Assemble the payment order document from a captured, validated context."""
    now = ctx.now
    oid = ctx.payment_oid

    return {
        "_id": oid,
        "paymentId": ctx.payment_id,
        "endToEndId": ctx.end_to_end_id,
        "instructionId": derive_ref("INSTR", oid),
        "txnId": derive_ref("TXN", oid),
        "uetr": f"UETR-{str(oid)}",
        "msgId": derive_ref("MSG", oid),
        "customerId": ctx.debtor_customer_id,
        "initiatedAt": now,
        # TODO (stage 3): hardcoded. `ctx.payment_type` is accepted and ignored.
        "type": "CREDIT_TRANSFER",
        "rail": ctx.payment_rail,
        "status": lifecycle.DRAFT,
        "priority": "NORMAL",
        "instructedAmount": ctx.instructed_amount,
        "instructedCurrency": ctx.instructed_currency,
        "amount": ctx.instructed_amount,
        "currency": ctx.instructed_currency,
        "chargeBearer": "SLEV",
        "fees": [],
        "debtor": party_snapshot(ctx.debtor_customer, ctx.debtor_account),
        "creditor": party_snapshot(ctx.creditor_customer, ctx.creditor_account),
        "remittance": {
            "unstructured": ctx.remittance_unstructured,
            "reference": None,
            "invoiceNo": None,
            # TODO (stage 3 enrichment): resolve against the purpose-code reference table.
            "purposeCode": None,
        },
        "correspondent": {
            # TODO (stage 3 enrichment): hardcoded CLEAR — no screening runs.
            "sanctionsCheck": {
                "status": "CLEAR",
                "checkedAt": now,
                "provider": "PROV-SYNTH",
            }
        },
        "cardTxn": None,
        "rtp": None,
        # The stage timeline. Doc 10 Part B: the slots already exist, so the state machine
        # populating them as each stage fires is free. Today all four are stamped at once
        # and only `settledAt` is written later, by stage 7.
        "clearing": {
            "receivedAt": now,
            "validatedAt": now,
            "authorisedAt": now,
            "submittedAt": now,
            "settledAt": None,
        },
        # Written by stage 4b, after the instruction exists (decision §11).
        "fraud": None,
        "initiation": {
            "initiatedAt": now,
            "initiatedBy": ctx.debtor_customer_id,
            "channel": "API",
            "ipAddress": None,
            "deviceId": None,
        },
        "isInternal": ctx.is_internal,
        "createdAt": now,
        "updatedAt": now,
        "createdBy": "SERVICE-PAYMENTS",
        "version": 1,
        "sourceSystem": "leafy-bank-payments-service",
        # The instruction is created at DRAFT and advanced from there (§11). Only
        # `lifecycle.advance` may change this block afterwards.
        "lifecycle": lifecycle.initial_block(
            actor="transactions-service",
            actor_type="SERVICE",
            reason="Payment instruction captured",
            now=now,
        ),
        # TODO (D3): refs{} forward pointers exist in the schema; nothing writes them yet.
    }
