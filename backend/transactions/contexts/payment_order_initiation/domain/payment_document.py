"""Builds the canonical `payments` document. Pure — no I/O, no clock.

BIAN PaymentOrderInitiation (SD 42933). The envelope shape is
`doinas-research/propose_payments.json`; field-level gaps against pain.001 are catalogued
in `07-schema-coverage-gaps.md`.

Pure by design: `persist.py` does the insert, this decides the shape. That split is what
lets the document shape be tested without a database, which matters because the envelope
migration (deferred, plan §9) will rewrite most of this.

Stage 1 writes the document at its full spec shape — every `required` key present, the
blocks later stages own present-but-null. A document born malformed is inherited malformed
by all nine stages, so the all-null block is not padding; it is the slot a later stage
fills without an `$exists`-branching read.

Known, deliberate deviations from the spec's `$jsonSchema` (doc 13 §2 B4):
  * `instructedAmount` / `amount` are Python floats, not `Decimal128`.
  * timestamps are `datetime` objects, not ISO-8601 strings.
Both are genuine conformance gaps and both are repo-wide type migrations reaching the
frontend. They are tracked separately; do NOT fix them here. The collection validator
stays unapplied until they are (and until `fraud` becomes nullable — Doina Q9).
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from contexts.payment_order_initiation.domain import initiation_envelope, lifecycle
from shared.refs import derive_ref

# The bank's own agent identity, stamped on whichever side of the payment we hold.
OUR_BIC = "LEAFUS33"
OUR_BANK_NAME = "Leafy Bank"
OUR_BANK_COUNTRY = "US"

SCHEMA_VERSION = 1

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


def _address_text(customer: dict) -> Optional[str]:
    """The spec types `address` as a string; the customer record holds a list of address
    objects. Take the first and flatten it — a party snapshot is a point-in-time label,
    not a queryable address record."""
    addresses = (customer.get("contact", {}) or {}).get("addresses", []) or []
    if not addresses:
        return None
    first = addresses[0]
    if isinstance(first, str):
        return first
    parts = [
        first.get(k)
        for k in ("line1", "line2", "city", "state", "postalCode", "country")
        if first.get(k)
    ]
    return ", ".join(parts) or None


def party_snapshot(customer: dict, account: dict) -> dict:
    """Debtor / creditor agent block for a party the bank holds, denormalised onto the
    payment at initiation time. Immutable afterwards (R6, FATF R.16): later changes to the
    customer record must not rewrite what the instruction said when it was given."""
    identification = customer.get("identification", {}) or {}
    return {
        "accountId": account["accountId"],
        "accountNo": account.get("accountNumber"),
        "iban": account.get("iban"),
        "name": identification.get("legalName"),
        "bic": OUR_BIC,
        "bankName": OUR_BANK_NAME,
        "bankCountry": OUR_BANK_COUNTRY,
        "address": _address_text(customer),
        "accountType": display_account_type(account.get("type")),
        # D10 — a domestic wire is unsendable without these. Null at initiation; stage 3
        # enrichment resolves them from the creditor's bank.
        "clearingSystemMemberId": None,
        "clearingSystemCode": None,
    }


def external_party_snapshot(party: dict) -> dict:
    """Creditor snapshot for a beneficiary the bank does NOT hold. Built straight from the
    request — there is no account to resolve. The spec makes `accountId` nullable and
    requires only `accountNo` + `name` on a creditor precisely for this case; the request
    contract enforces both (`PaymentOrderInitiateRequest`)."""
    return {
        "accountId": None,
        "accountNo": party.get("accountNo"),
        "iban": party.get("iban"),
        "name": party.get("name"),
        "bic": party.get("bic"),
        "bankName": party.get("bankName"),
        "bankCountry": party.get("bankCountry"),
        "address": party.get("address"),
        "accountType": party.get("accountType"),
        "clearingSystemMemberId": party.get("clearingSystemMemberId"),
        "clearingSystemCode": party.get("clearingSystemCode"),
    }


def creditor_snapshot(ctx) -> dict:
    if ctx.is_external_creditor:
        return external_party_snapshot(ctx.creditor_party or {})
    return party_snapshot(ctx.creditor_customer, ctx.creditor_account)


def _iso_date(value: Optional[date]) -> Optional[str]:
    """`requestedExecutionDate` is an ISO-8601 date string (YYYY-MM-DD) in the spec, not a
    datetime — it is a calendar intent, not an instant."""
    return value.isoformat() if value else None


def build(ctx) -> dict:
    """Assemble the payment order document from a captured, validated context."""
    now = ctx.now
    oid = ctx.payment_oid

    creditor = creditor_snapshot(ctx)
    debtor = party_snapshot(ctx.debtor_customer, ctx.debtor_account)

    envelopes = initiation_envelope.build_envelopes(
        rail=ctx.payment_rail,
        payment_oid=oid,
        is_own_account=ctx.is_internal,
        debtor_bank_country=debtor.get("bankCountry"),
        creditor_bank_country=creditor.get("bankCountry"),
        wire_details=ctx.wire_details,
        ach_details=ctx.ach_details,
        internal_details=ctx.internal_details,
    )

    doc = {
        "_id": oid,
        "paymentId": ctx.payment_id,
        # Pure ISO 20022 EndToEndIdentification. Idempotency is `idempotencyKey`'s job —
        # conflating the two (R7) made a caller's retry key part of the payment's public
        # identity.
        "endToEndId": ctx.end_to_end_id,
        "idempotencyKey": ctx.idempotency_key,
        "instructionId": derive_ref("INSTR", oid),
        "txnId": derive_ref("TXN", oid),
        "uetr": f"UETR-{str(oid)}",
        "msgId": derive_ref("MSG", oid),
        "customerId": ctx.debtor_customer_id,
        "initiatedAt": now,
        # R3: the customer's selection, set once at initiation and never inferred later.
        "type": ctx.payment_type,
        "rail": ctx.payment_rail,
        "status": lifecycle.DRAFT,
        "priority": ctx.priority,
        "instructedAmount": ctx.instructed_amount,
        "instructedCurrency": ctx.instructed_currency,
        "amount": ctx.instructed_amount,
        "currency": ctx.instructed_currency,
        "chargeBearer": ctx.charge_bearer,
        "categoryPurpose": ctx.category_purpose,
        # R8 — captured at the entry screen for EVERY rail. Single source of truth: the
        # rail mappers project it outward (pain.001 ReqdExctnDt, NACHA effectiveEntryDate)
        # rather than each envelope holding its own copy.
        "requestedExecutionDate": _iso_date(ctx.requested_execution_date),
        "fees": [],
        "debtor": debtor,
        "creditor": creditor,
        "remittance": {
            "unstructured": ctx.remittance_unstructured,
            "reference": ctx.remittance_reference,
            "invoiceNo": ctx.remittance_invoice_no,
            # TODO (stage 3 enrichment): resolve against the purpose-code reference table.
            "purposeCode": None,
        },
        "correspondent": {
            "correspondentBic": None,
            "intermediaryBic": None,
            # Spec-required array. Empty at initiation; stage 3/4 append.
            "regulatoryReports": [],
            # TODO (stage 3 enrichment): hardcoded CLEAR — no screening runs.
            "sanctionsCheck": {
                "status": "CLEAR",
                "checkedAt": now,
                "provider": "PROV-SYNTH",
            },
        },
        # Required objects, not nullable. All-null bodies until a CARD / RTP rail
        # populates them. `rtp.isRFP` is a required non-nullable bool -> false.
        "cardTxn": {
            "cardId": None,
            "maskedPan": None,
            "network": None,
            "txnType": None,
            "merchantId": None,
            "merchantName": None,
            "mcc": None,
            "terminalId": None,
            "authCode": None,
            "isCardPresent": None,
            "isEcommerce": None,
        },
        "rtp": {
            "network": None,
            "rfpId": None,
            "proxyType": None,
            "proxyValue": None,
            "isRFP": False,
        },
        # The stage timeline, and the demo renders it. Only the two timestamps stage 1
        # has actually earned are stamped: `authorisedAt` belongs to stage 4 and
        # `submittedAt` to stage 5. Stamping all four at creation made the timeline a
        # fiction — the spec's own `wire_domestic` sample has them null at VALIDATED.
        "clearing": {
            "receivedAt": now,
            "validatedAt": now,
            "authorisedAt": None,
            "submittedAt": None,
            "settlementDate": None,
            "settledAt": None,
            "batchRef": None,
            "networkRef": None,
            "networkCode": None,
            "statusCode": None,
            "rejectionCode": None,
            "returnCode": None,
        },
        # Written by stage 4b, after the instruction exists (decision §11).
        "fraud": None,
        # Stage 2's two slots, present-but-empty for the same reason as every other block
        # here: a later stage fills them without an `$exists`-branching read. `checks[]`
        # is append-only and shared with stage 3 onward (`domain/checks.py`); both are
        # additive and not `required` in the spec, pending Doina Q12.
        "checks": [],
        "authentication": None,
        "entitlement": None,
        "initiation": {
            "initiatedAt": now,
            "initiatedBy": ctx.debtor_customer_id,
            "channel": ctx.channel,
            "ipAddress": None,
            "deviceId": None,
        },
        # D3 — forward pointers into the documents later stages produce. Written all-null
        # here so a consumer can project `refs.journalEntryId` without an $exists branch.
        "refs": {
            "paymentOrderId": None,
            "routingSnapshotId": None,
            "paymentExecutionIds": [],
            "transactionId": None,
            "ledgerEventId": None,
            "journalEntryId": None,
            "canonicalJsonId": None,
        },
        # Not in the spec, and legal (it declares no `additionalProperties: false`).
        # Written today, left alone deliberately — removing them is unrelated to stage 1.
        "isInternal": ctx.is_internal,
        "createdBy": "SERVICE-PAYMENTS",
        "createdAt": now,
        "updatedAt": now,
        "version": 1,
        "sourceSystem": "leafy-bank-payments-service",
        "schemaVersion": SCHEMA_VERSION,
        # The instruction is created at DRAFT and advanced from there (§11). Only
        # `lifecycle.advance` may change this block afterwards.
        "lifecycle": lifecycle.initial_block(
            actor="transactions-service",
            actor_type="SERVICE",
            reason="Payment instruction captured",
            now=now,
        ),
    }
    doc.update(envelopes)
    return doc
