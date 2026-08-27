"""Stage 1 — capture the payment instruction (BIAN PaymentOrderInitiation, SD 42933).

Doina stage 1: *"The canonical payments instruction is created immediately."* This stage
resolves the instruction, **persists it at DRAFT**, and advances it to INITIATED. Every
later stage advances the state on a document that already exists, so a rejection is a
terminal state on a real payment rather than the absence of one (decision §11).

The basic-field guards below run **before** the insert. Doina's Stage 1 Key Features include
*"Perform basic fields validation"* — a malformed amount is refused before an instruction
exists. Account and funds validation is stage 3's job, on the persisted document.

Reads  ctx: customer_ref, debtor_account_ref, creditor_account_ref, instructed_amount,
            instructed_currency, payment_rail, idempotency_key, collections
Writes ctx: now, payment_oid, payment_id, end_to_end_id, txn_code, is_internal,
            debtor_account, creditor_account, debtor_customer, creditor_customer,
            debtor_customer_id, creditor_customer_id, payment_doc, current_state
            (or halts on replay)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from contexts.payment_order_initiation.domain import lifecycle, payment_document
from process.payment_context import PaymentContext
from shared.refs import derive_ref

logger = logging.getLogger(__name__)


def run(ctx: PaymentContext) -> None:
    c = ctx.collections

    # Intake sanity. Kept ahead of every lookup so a malformed request costs no I/O.
    if ctx.instructed_amount <= 0:
        raise ValueError("PaymentInstructedAmount must be greater than 0.")
    if ctx.instructed_amount > ctx.payment_limit_usd:
        raise ValueError(
            f"PaymentInstructedAmount exceeds the limit of {ctx.payment_limit_usd}."
        )

    # Idempotent replay. This pre-check is a courtesy, not the guarantee — the unique
    # index on `endToEndId` is what enforces idempotency, and `persist.py` honours it.
    if ctx.idempotency_key:
        existing = c.payments.find_one({"endToEndId": ctx.idempotency_key})
        if existing:
            logger.info(
                "Idempotent replay for endToEndId=%s — returning existing paymentId=%s",
                ctx.idempotency_key,
                existing["paymentId"],
            )
            ctx.stop(existing)
            return

    # Resolve the accounts the instruction names.
    ctx.debtor_account = c.accounts.find_one({"accountId": ctx.debtor_account_ref})
    if not ctx.debtor_account:
        raise ValueError(f"Debtor account {ctx.debtor_account_ref} not found.")
    ctx.creditor_account = c.accounts.find_one({"accountId": ctx.creditor_account_ref})
    if not ctx.creditor_account:
        raise ValueError(f"Creditor account {ctx.creditor_account_ref} not found.")

    # v6: the customer FK lives at customerSnapshot.customerId (top-level customerId removed).
    ctx.debtor_customer_id = ctx.debtor_account["customerSnapshot"]["customerId"]
    ctx.creditor_customer_id = ctx.creditor_account["customerSnapshot"]["customerId"]

    ctx.debtor_customer = c.customers.find_one({"customerId": ctx.debtor_customer_id})
    ctx.creditor_customer = c.customers.find_one({"customerId": ctx.creditor_customer_id})
    if not ctx.debtor_customer or not ctx.creditor_customer:
        raise ValueError("Customer reference data missing for debtor or creditor.")

    ctx.is_internal = ctx.debtor_customer_id == ctx.creditor_customer_id

    # Identifiers. One ObjectId seeds every typed ref so they correlate across collections.
    ctx.payment_oid = ObjectId()
    ctx.payment_id = derive_ref("PAY", ctx.payment_oid)
    ctx.end_to_end_id = ctx.idempotency_key or derive_ref("E2E", ctx.payment_oid, last_n=12)
    # ISO 20022 BankTransactionCode derived from rail.
    ctx.txn_code = "PMNT-ICDT-BOOK" if ctx.payment_rail == "INTERNAL" else "PMNT-ICDT-ESCT"

    # One timestamp for the instruction. Later stages stamp their own transition times.
    ctx.now = datetime.now(timezone.utc)

    # --- persist at DRAFT ----------------------------------------------------
    # Outside any ACID transaction: an order initiation is a distinct business step
    # (2026-06-18 decision), and it must survive a later validation failure so the
    # rejection is traceable.
    ctx.payment_doc = payment_document.build(ctx)
    try:
        c.payments.insert_one(ctx.payment_doc)
    except DuplicateKeyError:
        # Lost a race against a concurrent identical request. The find_one pre-check above
        # cannot serialise these — the unique index on endToEndId is what actually enforces
        # idempotency, and this is the path that honours it. Return the winner's document so
        # both callers see the same payment rather than failing the loser with a spurious 400.
        existing = c.payments.find_one({"endToEndId": ctx.end_to_end_id})
        if existing is None:
            raise
        logger.info(
            "Concurrent idempotent replay for endToEndId=%s — returning existing paymentId=%s",
            ctx.end_to_end_id,
            existing["paymentId"],
        )
        ctx.stop(existing)
        return

    ctx.current_state = lifecycle.DRAFT
    lifecycle.advance_ctx(
        ctx, lifecycle.INITIATED,
        actor="transactions-service",
        reason="Instruction captured",
    )
