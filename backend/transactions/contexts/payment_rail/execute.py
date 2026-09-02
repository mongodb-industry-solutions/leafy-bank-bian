"""Stage 5 — execution (BIAN PaymentRail, SD 47741; Financial Gateway, SD 30542).

Doina L533-565. Two paths, and the split is hers:

* **rail-bound** (`WIRE` in Phase 1) — *"transform the canonical payment into an ISO 20022
  message **only at the rail boundary**"* (L542). Canonical payment -> mapper -> pacs.008 ->
  gateway -> acknowledgement, then the artifacts that record all of it.
* **book transfer** (`INTERNAL`) — reaches no rail, so it maps nothing and writes neither
  artifact. Two independent pieces of evidence for that: her *"only at the rail boundary"*,
  and the spec's own `internal_transfer` sample, which sits at **SETTLED** with
  `refs.paymentExecutionIds: []` and `canonicalJsonId: null`. Doc 19 B4.

## The ACID block — do not restructure

`_money_move` below is moved **unmodified** from `payments_service.py:201-277`. It performs
the money move as ONE multi-document ACID transaction: debtor balance, creditor balance,
one `transactions` doc, one notification, and the payment status flip.

It spans four aggregates — debtor account, creditor account, payment, notification — which
textbook DDD says to split into a saga. **We keep it.** Multi-document ACID across
aggregates *is the MongoDB capability being demoed* (doc 09 §5). Documented here so nobody
"corrects" it, and so the exception stays one block long instead of spreading.

## Why the SETTLED transition is in here

For a synchronous internal book transfer, settlement **is** the money move — so the
`SETTLED` transition belongs inside this transaction, atomic with the two balance updates.
That is a design choice, not a leftover: a payment can never be observed as settled without
the balances having moved, or vice versa.

When stage 7 becomes real (nostro/vostro, value dates, rail confirmation), settlement stops
being simultaneous and this transition moves to `payment_settlement/settle.py`. The state
machine is what makes that a one-line change.

## External beneficiaries halt at IN_PROGRESS — and the halt is NOT stage 5's to remove

Stage 1 captures a payment to a beneficiary Leafy Bank does not hold; the spec makes
`creditor.accountId` nullable and Doina's flagship `wire_domestic` scenario is exactly that
shape. Such a payment now runs the **whole** of stage 5 — mapping, submission,
acknowledgement, both artifacts — and then stops before the money move.

⚠️ **An earlier version of this docstring said "when stage 5 lands, delete the guard". That
instruction is retracted** (doc 19 B1): what the guard waits on is the **chart-of-accounts
extension** (`Dr Customer Deposit Liability / Cr Wire Clearing Account`), which is her stage 7
and Doina/Payton's call (doc 08, Q8) — stage 5 does not bring it. The blocker *splits*:
message generation and execution recording are this stage's; crediting anything is not.

Concretely, what would happen without the halt: `_money_move` credits the creditor by
`accountId`, so with no account the credit matches nothing, the assertion at the end of the
callback aborts the transaction, and the payment ends FAILED mid-demo. Remove the assertion
too and it is worse — the debtor is debited, the credit is lost, and
`transactions.payee.accountId` is null, which makes the ledger's `ingest_worker` raise and
crash-loop (defect 2026-07-01, hit twice).

`IN_PROGRESS`, not a rejection: the rail has accepted the message and settlement is pending,
which is a real wire's actual state between submission and confirmation. It is also inside
`_POST_EXECUTION_TERMINALS` (`lifecycle.py:75`), so from here the payment can only be
RETURNED / REVERSED / FAILED / REFUNDED — never REJECTED, which is correct once a message has
left the bank.

## Gated on stage 4 (R9)

Her L538: *"This step depends on the result of Stage 4's authorization decision."* The saga
already enforces it structurally — a `REVIEW` holds at `AUTHORISED` and a `DECLINED` never
arrives — but the precondition is asserted here anyway, so an invariant that currently holds
by luck of ordering holds by test instead.

Reads  ctx: everything resolved and produced by stages 1-4; rail_gateway
Writes ctx: current_state -> SUBMITTED -> IN_PROGRESS [-> SETTLED]; result,
            payment_execution_id, payment_message_id; `paymentExecutions`,
            `paymentMessages`; `payments.clearing.*`, `payments.refs.canonicalJsonId`,
            `payments.refs.paymentExecutionIds[]`; five `payments.checks[]` entries

TODO (Phase 2, and unreachable until then — `rail_viability.PHASE_1_RAILS`):
  - `adapters/ach_nacha.py` (US corridors; IAT cross-border) and `adapters/card_iso8583.py`,
    as further implementations of `ports/rail.RailGateway`. Three implementations of one port
    is the structural form of the canonical-envelope premise (doc 09 §3) — if ACH ends up
    needing its own context, the envelope has failed.
  - ACH's own status sequence (`QUEUED`/`BATCHED`/`ACCEPTED`) has no home in `currentState`;
    `paymentExecutions.railStatus` is the intended one. Q7.
  - the warehouse **release** scheduler (her L531). Stage 4 holds a future-dated payment at
    ROUTED; nothing releases it yet, so it never reaches this stage (doc 18 B9).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from pymongo.client_session import ClientSession

from contexts.payment_order_initiation.domain import checks, lifecycle
from contexts.payment_rail import documents
from contexts.payment_rail.domain import execution_documents, pacs008
from shared.refs import derive_ref
from process.payment_context import PaymentContext

logger = logging.getLogger(__name__)

STAGE = "5 execute"

# Rails that address an external network, and therefore run the mapper. Phase 1 is wires;
# `rail_viability.PHASE_1_RAILS` refuses ACH/CARD/RTP in stage 3, so this set is deliberately
# narrower than the `rail` enum rather than pretending to cover it.
RAIL_BOUND = frozenset({"WIRE"})

# The spec's own values for a book transfer, from the `internal_transfer` sampleDocument.
# Copied rather than invented so that sample stays reproducible end to end (doc 19 B4).
INTERNAL_NETWORK_REF = "INTERNAL-BOOK-TRANSFER"
INTERNAL_NETWORK_CODE = "0000"


def run(ctx: PaymentContext) -> None:
    now = datetime.now(timezone.utc)
    payment = ctx.payment_doc or {}
    recorded: list = []

    def record(name: str, result: str, detail: str, *, mode: str = checks.SYNC) -> None:
        recorded.append(
            checks.check(STAGE, name, result, mode=mode, detail=detail,
                         actor="payment-rail-service", at=now)
        )

    def refuse(name: str, detail: str) -> None:
        """Record, flush, then raise — the flush must precede the raise.

        The saga catches `ValueError` and terminates the payment, so a check written after
        that point would never exist. Same shape as `orchestrate.refuse`.
        """
        record(name, checks.FAIL, detail)
        _flush(ctx, recorded)
        raise ValueError(detail)

    # --- 1. authorization_confirmed (R9) -------------------------------------
    if ctx.current_state != lifecycle.APPROVED:
        refuse(
            "authorization_confirmed",
            f"Execution requires an approved payment; this one is at "
            f"{ctx.current_state}. Stage 4's authorization decision is the precondition "
            "for reaching the rail (her L538).",
        )
    record(
        "authorization_confirmed", checks.PASS,
        "Stage 4 authorization decision APPROVED — the bank has committed to execute "
        f"(payment order {(payment.get('refs') or {}).get('paymentOrderId')}).",
    )

    if ctx.payment_rail not in RAIL_BOUND:
        _execute_internal(ctx, record, recorded, now)
        return

    _execute_rail_bound(ctx, record, recorded, now)


# --------------------------------------------------------------------------- #

def _execute_internal(ctx: PaymentContext, record, recorded: list,
                      now: datetime) -> None:
    """The book transfer: no message, no artifacts, and the spec's own clearing values.

    The two SKIPs are the demo's own explanation of why an internal transfer is cheaper —
    D9's point, made by the document rather than by a slide.
    """
    strategy = ctx.execution_strategy
    record(
        "iso20022_message_generated", checks.SKIP,
        f"Rail {ctx.payment_rail} reaches no external network — a book transfer crosses no "
        "rail boundary, so no ISO 20022 message is generated (her L542).",
    )
    record(
        "canonical_payload_stored", checks.SKIP,
        "No rail message to store a canonical payload for.",
    )
    record(
        "rail_submission_acknowledged", checks.SKIP,
        "Settled on our own books — there is no rail to acknowledge the payment.",
    )
    record(
        "execution_recorded", checks.SKIP,
        "No execution artifact: the spec's own settled internal transfer records "
        "`paymentExecutionIds: []` (doc 19 B4, Q39).",
    )
    _flush(ctx, recorded)

    value_date = getattr(strategy, "value_date", None)
    lifecycle.advance_ctx(
        ctx, lifecycle.SUBMITTED,
        actor="payment-rail-service",
        reason=f"Submitted to {ctx.payment_rail} rail",
        extra={
            "clearing.submittedAt": now,
            "clearing.networkRef": INTERNAL_NETWORK_REF,
            "clearing.networkCode": INTERNAL_NETWORK_CODE,
            "clearing.settlementDate": value_date,
        },
    )
    lifecycle.advance_ctx(
        ctx, lifecycle.IN_PROGRESS,
        actor="payment-rail-service",
        reason="Rail execution started",
    )
    ctx.result = _money_move(ctx)


def _execute_rail_bound(ctx: PaymentContext, record, recorded: list,
                        now: datetime) -> None:
    """Her L544 pipeline, in her order: canonical -> mapper -> pacs.008 -> rail -> clearing."""
    payment = ctx.payment_doc or {}
    strategy = ctx.execution_strategy
    correspondent = payment.get("correspondent") or {}

    # --- both ids first, so each document carries the other's ref at insert --
    # `derive_ref` is deterministic from the oid, so pre-minting removes the only
    # forward-reference update either collection would otherwise need (her L892 asks for
    # `paymentExecutionId` on the message).
    message_oid, execution_oid = ObjectId(), ObjectId()
    message_id = derive_ref("PM", message_oid)
    execution_id = derive_ref("PE", execution_oid)

    # --- 2. iso20022_message_generated (R1, R3, R5) --------------------------
    settlement_mtd = pacs008.settlement_method(
        clearing_network=getattr(strategy, "network", None),
        via_correspondent=bool(correspondent.get("correspondentBic")),
    )
    message = pacs008.build(
        payment,
        settlement_mtd=settlement_mtd,
        # Stage 4 decided the value date; `clearing.settlementDate` is not stamped until the
        # acknowledgement below, so the strategy is the only source at this point.
        value_date=getattr(strategy, "value_date", None),
    )
    produced = pacs008.elements(message)
    record(
        "iso20022_message_generated", checks.PASS,
        f"{pacs008.MESSAGE_FORMAT} built at the rail boundary from the canonical payment "
        f"({len(produced)} elements, mapping {pacs008.MAPPING_VERSION}, "
        f"settlement method {settlement_mtd}).",
    )

    # --- 3. canonical_payload_stored (R7) ------------------------------------
    _insert(
        ctx, "payment_messages", execution_documents.PAYMENT_MESSAGES,
        execution_documents.payment_message(
            payment=payment,
            message=message,
            payment_execution_id=execution_id,
            settlement_mtd=settlement_mtd,
            now=now,
            oid=message_oid,
        ),
    )
    ctx.payment_message_id = message_id
    record(
        "canonical_payload_stored", checks.PASS,
        f"{message_id} holds the normalised payload, the mapping version and the "
        "transformation audit — the message/protocol representation, kept separate from "
        "the business payment (her L887-892).",
    )

    # --- 4. execution_recorded (R8) ------------------------------------------
    execution = execution_documents.payment_execution(
        payment=payment,
        strategy=strategy,
        message=message,
        payment_message_id=message_id,
        attempt=_next_attempt(ctx),
        now=now,
        oid=execution_oid,
    )
    _insert(ctx, "payment_executions", execution_documents.PAYMENT_EXECUTIONS, execution)
    ctx.payment_execution_id = execution_id
    record(
        "execution_recorded", checks.PASS,
        f"{execution_id} records attempt {execution['attempt']} against "
        f"{execution['clearingNetwork'] or ctx.payment_rail}, with the message as sent. "
        "Append-only: a repair, recall, return or retry inserts a further attempt.",
    )

    _flush(ctx, recorded)

    lifecycle.advance_ctx(
        ctx, lifecycle.SUBMITTED,
        actor="payment-rail-service",
        reason=(
            f"{pacs008.MESSAGE_FORMAT} submitted to "
            f"{getattr(strategy, 'network', None) or ctx.payment_rail}"
        ),
        extra={
            "clearing.submittedAt": now,
            "refs.canonicalJsonId": message_id,
        },
    )
    # `paymentExecutionIds` is an array because executions are append-only — so it is
    # pushed, never set. The spec's own field description says exactly that.
    ctx.collections.payments.update_one(
        {"_id": ctx.payment_oid},
        {"$push": {"refs.paymentExecutionIds": execution_id}},
    )

    # --- 5. rail_submission_acknowledged (R10, R13) --------------------------
    ack = ctx.rail_gateway.submit(
        message,
        network=getattr(strategy, "network", None),
        payment_id=payment.get("paymentId"),
    )
    _acknowledge(ctx, execution_oid, ack, now)

    if not ack.accepted:
        # The rail refused the message. Not the caller's error and not a fraud decision, so
        # it terminates the payment as FAILED rather than REJECTED — and the attempt survives
        # on `paymentExecutions` with the rail's own reason, which is what stage 9 repairs
        # from. The check is recorded before the raise, per `refuse`.
        recorded.append(
            checks.check(
                STAGE, "rail_submission_acknowledged", checks.FAIL,
                detail=f"{ack.status_code}: {ack.reason}",
                actor="payment-rail-service", at=now,
            )
        )
        _flush(ctx, recorded)
        raise ValueError(f"Rail rejected the payment message — {ack.status_code}: {ack.reason}")

    recorded.append(
        checks.check(
            STAGE, "rail_submission_acknowledged", checks.PASS,
            detail=(
                f"{ack.status_code} from {getattr(strategy, 'network', None)} "
                f"(SIMULATED) — network reference {ack.network_ref}, "
                f"status message {ack.message_ref}."
            ),
            actor="payment-rail-service", at=now,
        )
    )
    _flush(ctx, recorded)

    payment_doc = lifecycle.advance_ctx(
        ctx, lifecycle.IN_PROGRESS,
        actor="payment-rail-service",
        reason=(
            f"Accepted by {getattr(strategy, 'network', None) or ctx.payment_rail} "
            f"({ack.status_code}); settlement pending"
        ),
        extra={
            "clearing.networkRef": ack.network_ref,
            "clearing.networkCode": ack.network_code,
            "clearing.statusCode": ack.status_code,
            "clearing.settlementDate": ack.settlement_date
            or getattr(strategy, "value_date", None),
        },
    )

    if ctx.is_external_creditor:
        # Halted here by design — see the module docstring. The rail holds the payment; the
        # credit leg needs the chart-of-accounts extension (her stage 7, Q8). No
        # `transactions` doc is written, so the ledger never observes this payment.
        ctx.stop(payment_doc)
        return

    ctx.result = _money_move(ctx)


def _next_attempt(ctx: PaymentContext) -> int:
    """1-based, counted from what is already stored.

    A retry must not overwrite attempt 1 (her L862), and the count is the one thing that
    cannot be derived from the payment document — `refs.paymentExecutionIds` would do, but
    counting the collection is correct even if a push was lost.
    """
    handle = _collection(ctx, "payment_executions", execution_documents.PAYMENT_EXECUTIONS)
    if handle is None:
        return 1
    return handle.count_documents({"paymentId": ctx.payment_id}) + 1


def _acknowledge(ctx: PaymentContext, execution_oid: ObjectId, ack, now: datetime) -> None:
    """The one permitted update on an execution attempt: the rail's answer to it.

    Three named fields, built by `execution_documents.acknowledge`. Anything else touching
    this collection is a bug, and the AST guard in the tests is what says so.
    """
    handle = _collection(ctx, "payment_executions", execution_documents.PAYMENT_EXECUTIONS)
    if handle is None:  # pragma: no cover - only a hand-built context lacks it
        return
    handle.update_one(
        {"_id": execution_oid},
        {"$set": execution_documents.acknowledge(ack, now=now)},
    )


def _insert(ctx: PaymentContext, attr: str, name: str, document: dict) -> None:
    """Insert-only. Neither collection has an update path except `_acknowledge`."""
    handle = _collection(ctx, attr, name)
    if handle is None:  # pragma: no cover - only a hand-built context lacks it
        return
    handle.insert_one(document)


def _collection(ctx: PaymentContext, attr: str, name: str):
    """The named handle, falling back to `db[name]`.

    Same pattern as `orchestrate._collection`: a context built before stage 5 existed carries
    neither handle, and reaching through `db` keeps it working rather than raising an
    AttributeError deep inside a stage.
    """
    collections = ctx.collections
    if collections is None:
        return None
    handle = getattr(collections, attr, None)
    if handle is not None:
        return handle
    db = getattr(collections, "db", None)
    return None if db is None else db[name]


def _flush(ctx: PaymentContext, recorded: list) -> None:
    checks.append_checks(ctx.collections.payments, ctx.payment_oid, recorded)
    recorded.clear()


def _debtor_borne_fee(ctx: PaymentContext) -> float:
    """The charge the debtor bears, for the ledger's fee leg (stage 6, doc 20 B3).

    Only `chargedTo == "DEBTOR"` is carried. `_FEE_PAYER_BY_CHARGE_BEARER` can produce a
    creditor-borne fee from an ISO `CRED`/`SHAR` charge bearer, and which account such a fee
    debits is Doina's call (Q44) — so it is skipped here rather than posted to the wrong
    account. Skipping is visible in the log, not silent.

    Returns 0.0 when there is no fee, which is every internal transfer: stage 3 levies a
    charge on `rail == "WIRE"` only.
    """
    total = 0.0
    skipped = []
    for fee in (ctx.payment_doc or {}).get("fees") or []:
        charged_to = fee.get("chargedTo")
        if charged_to == "DEBTOR":
            total += float(fee.get("amount") or 0.0)
        else:
            skipped.append(f"{fee.get('type')} chargedTo={charged_to}")
    if skipped:
        logger.info(
            "payment %s: %d fee(s) not carried to the ledger (%s) — only debtor-borne "
            "charges post in phase 1 (Q44)",
            ctx.payment_id, len(skipped), "; ".join(skipped),
        )
    return total


def _money_move(ctx: PaymentContext) -> dict:
    c = ctx.collections
    now = ctx.now
    amount = ctx.instructed_amount

    def callback(session: ClientSession) -> dict:
        debtor_after = c.accounts.find_one_and_update(
            {
                "accountId": ctx.debtor_account_ref,
                "balance.available": {"$gte": amount},
            },
            {
                "$inc": {
                    "balance.current": -amount,
                    "balance.available": -amount,
                    "balance.ledger": -amount,
                },
                "$set": {"balance.updatedAt": now, "updatedAt": now},
            },
            session=session,
            return_document=True,
        )
        if debtor_after is None:
            raise ValueError("Insufficient funds at settlement time.")
        creditor_after = c.accounts.find_one_and_update(
            {"accountId": ctx.creditor_account_ref},
            {
                "$inc": {
                    "balance.current": amount,
                    "balance.available": amount,
                    "balance.ledger": amount,
                },
                "$set": {"balance.updatedAt": now, "updatedAt": now},
            },
            session=session,
            return_document=True,
        )
        # A no-match here means the debit committed and the credit did not — money
        # destroyed. The external-beneficiary guard in `run` makes this unreachable, and
        # this assertion is what keeps it that way: it aborts the transaction instead of
        # silently succeeding. Never remove it to "handle" a missing creditor.
        if creditor_after is None:
            raise ValueError(
                f"Creditor account {ctx.creditor_account_ref} did not match at settlement "
                "time — the money move was rolled back."
            )

        txn_doc = documents.transaction_doc(
            payment_oid=ctx.payment_oid,
            payment_id=ctx.payment_id,
            debtor_account=ctx.debtor_account,
            debtor_customer=ctx.debtor_customer,
            creditor_account=ctx.creditor_account,
            creditor_customer=ctx.creditor_customer,
            debtor_after=debtor_after,
            amount=amount,
            currency=ctx.instructed_currency,
            payment_rail=ctx.payment_rail,
            txn_code=ctx.txn_code,
            is_internal=ctx.is_internal,
            payment_execution_id=ctx.payment_execution_id,
            fee_amount=_debtor_borne_fee(ctx),
            fee_currency=ctx.instructed_currency,
            now=now,
        )
        c.transactions.insert_one(txn_doc, session=session)

        notif_docs = documents.build_notifications(
            payment_oid=ctx.payment_oid,
            payment_id=ctx.payment_id,
            txn_id=txn_doc["txnId"],
            debtor_account=ctx.debtor_account,
            creditor_account=ctx.creditor_account,
            debtor_customer=ctx.debtor_customer,
            debtor_after=debtor_after,
            amount=amount,
            currency=ctx.instructed_currency,
            payment_rail=ctx.payment_rail,
            is_internal=ctx.is_internal,
            now=now,
        )
        if notif_docs:
            c.notifications.insert_many(notif_docs, session=session)

        # Settlement, atomic with the money move. See the module docstring.
        return lifecycle.advance_ctx(
            ctx, lifecycle.SETTLED,
            actor="transactions-service",
            reason="Book transfer settled",
            session=session,
            extra={"clearing.settledAt": now},
        )

    with c.db.client.start_session() as session:
        return session.with_transaction(callback)
