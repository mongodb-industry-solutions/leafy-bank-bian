"""The incoming wire — inbound stages 1-8 (Doina Sep 17, the nine `## Incoming` sections).

Named "stage ten" because it is the tenth build, not a tenth stage: it spans her stages 1-9
in the inbound direction. Fixtures are reused from `test_payments_service` so an inbound
payment is exercised against exactly the accounts and customers the outbound path uses —
per the fixture-fidelity rule, the fixture must be the production shape, not a convenient one.

## What each section holds

1. the parser — round-tripped against the REAL mapper, never hand-built literals
2. the message documents — ordering, direction, and the "not a second payment" contract
3. the happy path — RECEIVED -> ... -> SETTLED, with the mirrored legs
4. beneficiary resolution — all three outcomes, driven through the real saga
5. UTA — Repair and Return, including the persistence that makes a resume correct
6. the generated messages — element NAMES checked against the standard (defect 2026-09-02)
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from contexts.financial_gateway.domain import (
    inbound_documents,
    inbound_pacs008,
    name_match,
    pacs002,
    pacs004,
)
from contexts.payment_rail.domain import pacs008
from process import exceptions as exc_module
from tests.test_payments_service import (  # noqa: F401 - fixtures are used by name
    CREDITOR,
    CUST_C,
    FakeCollection,
    FakeConnection,
    FakeDb,
    _account,
    _customer,
    _CLEARING_WIRE,
    db,
    service,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

# The account the fixture's CREDITOR maps to, and the name on record for it. Read from the
# fixture builders rather than restated, so a fixture change cannot silently desync these.
_HOLDER_NAME = _customer(CUST_C)["identification"]["legalName"]
_ACCOUNT_NO = _account(CREDITOR, CUST_C)["accountNumber"]
# ⚠️ The two fixture accounts share an `accountNumber` (both end "01", and the builder
# derives the number from that suffix) but have DISTINCT IBANs. So the message names the
# beneficiary by IBAN, which is both what a cross-border wire actually carries and the only
# identifier that resolves unambiguously here. `_find_claimed_account` tries IBAN first for
# exactly this reason; the account-number fallback is exercised by the unknown-account test.
_IBAN = _account(CREDITOR, CUST_C)["iban"]


def _message(*, creditor_name=_HOLDER_NAME, iban=_IBAN, account_no=None,
             amount=25_000.0, currency="USD", debtor_name="Acme GmbH",
             debtor_bic="DEUTDEFF", uetr="UETR-inbound-001", purpose="SUPP"):
    """An inbound pacs.008, built BY THE REAL MAPPER.

    ⚠️ Deliberately not a hand-written literal. A fixture written from the same reading of
    the standard as the parser cannot fail when both are wrong together — that is defect
    2026-09-02 (`fixture-fidelity`), and the only defence is to generate the message with
    the production mapper and parse it with the production parser.

    ⚠️ One fidelity limit worth naming: `pacs008.build` always stamps OUR bank as `DbtrAgt`
    (it maps outbound payments, where we are always the debtor's agent). A real inbound
    message names the SENDING bank there. `_fix_debtor_agent` rewrites that one element so
    the fixture is an inbound message rather than one of ours read backwards — without it,
    every test would screen our own BIC as the originator and the sanctions tests would be
    meaningless.
    """
    payment = {
        "msgId": "MSG-SENDER-001",
        "initiatedAt": NOW,
        "instructionId": "INSTR-SENDER-001",
        "endToEndId": "E2E-SENDER-001",
        "txnId": "TXN-SENDER-001",
        "uetr": uetr,
        "amount": amount,
        "currency": currency,
        "chargeBearer": "SHAR",
        "requestedExecutionDate": "2026-09-28",
        "debtor": {
            "name": debtor_name, "accountNo": "DE89370400440532013000",
            "bic": debtor_bic, "bankName": "Deutsche Bank", "address": "Berlin, DE",
        },
        "creditor": {
            "name": creditor_name, "accountNo": account_no, "iban": iban,
            "bic": "LEAFUS33", "bankName": "Leafy Bank", "address": "New York, US",
        },
        "remittance": {"unstructured": "Invoice 4471", "reference": "REF-4471",
                       "purposeCode": purpose},
        "correspondent": {}, "clearing": {}, "wireDetails": {},
    }
    return _fix_debtor_agent(pacs008.build(payment), debtor_bic, "Deutsche Bank")


def _fix_debtor_agent(message, bic, bank_name):
    """Make the message's `DbtrAgt` the SENDING bank, as a real inbound message has it.

    See `_message`'s docstring: the outbound mapper stamps our own identity there because
    outbound we always are the debtor's agent. Rewritten here rather than hand-building the
    whole message, so every OTHER element still comes from the production mapper.
    """
    transaction = pacs008.body(message)["CdtTrfTxInf"][0]
    transaction["DbtrAgt"] = {"FinInstnId": {"BICFI": bic, "Nm": bank_name}}
    return message


def _payment_doc(db, payment_id=None):
    docs = list(db["payments"].find({}))
    if payment_id:
        return next(d for d in docs if d["paymentId"] == payment_id)
    assert len(docs) == 1, f"expected one payment, got {len(docs)}"
    return docs[0]


# =============================================================================
# 1. the parser
# =============================================================================

def test_the_parser_round_trips_the_real_mappers_output():
    """Every canonical value survives build -> parse. The strongest check available that the
    parser reads the structure the mapper writes, rather than a structure either invented."""
    parsed = inbound_pacs008.parse(_message())

    assert parsed["amount"] == 25_000.0
    assert parsed["currency"] == "USD"
    # The mapper strips the internal `UETR-` prefix, so the parsed message carries the bare id.
    assert parsed["uetr"] == "inbound-001"
    assert parsed["chargeBearer"] == "SHAR"
    assert parsed["purposeCode"] == "SUPP"
    assert parsed["remittanceUnstructured"] == "Invoice 4471"
    assert parsed["remittanceReference"] == "REF-4471"
    assert parsed["senderMsgId"] == "MSG-SENDER-001"
    assert parsed["senderEndToEndId"] == "E2E-SENDER-001"
    assert parsed["debtor"]["name"] == "Acme GmbH"
    assert parsed["debtor"]["bic"] == "DEUTDEFF"
    assert parsed["claimedCreditor"]["name"] == _HOLDER_NAME
    assert parsed["claimedCreditor"]["iban"] == _IBAN
    # Fixed by the channel, never chosen (her L385).
    assert parsed["rail"] == "WIRE"


def test_the_parser_refuses_a_message_it_cannot_build_a_payment_from():
    """Structural validation only — and each refusal names what is missing."""
    with pytest.raises(inbound_pacs008.MessageRejected, match="Not a pacs.008"):
        inbound_pacs008.parse({"Document": {"SomethingElse": {}}})

    no_amount = copy.deepcopy(_message())
    del pacs008.body(no_amount)["CdtTrfTxInf"][0]["IntrBkSttlmAmt"]
    with pytest.raises(inbound_pacs008.MessageRejected, match="IntrBkSttlmAmt"):
        inbound_pacs008.parse(no_amount)

    no_creditor = copy.deepcopy(_message())
    del pacs008.body(no_creditor)["CdtTrfTxInf"][0]["CdtrAcct"]
    with pytest.raises(inbound_pacs008.MessageRejected, match="creditor account"):
        inbound_pacs008.parse(no_creditor)


def test_a_bulk_message_is_refused_rather_than_half_applied():
    """Applying only the first of several credit transfers would LOSE MONEY silently.

    Refusing is the honest answer until bulk inbound is a feature — and the refusal names
    the count, so the operator can see what arrived.
    """
    bulk = copy.deepcopy(_message())
    body = pacs008.body(bulk)
    body["CdtTrfTxInf"].append(copy.deepcopy(body["CdtTrfTxInf"][0]))
    with pytest.raises(inbound_pacs008.MessageRejected, match="2 credit transfers"):
        inbound_pacs008.parse(bulk)


def test_the_idempotency_key_prefers_the_uetr_and_never_fabricates_one():
    """A fabricated key would be unique every time — silently disabling dedupe, which is
    worse than having none."""
    parsed = inbound_pacs008.parse(_message())
    assert inbound_pacs008.idempotency_key(parsed) == "INBOUND-inbound-001"

    no_uetr = inbound_pacs008.parse(_message(uetr=None))
    assert inbound_pacs008.idempotency_key(no_uetr) == (
        "INBOUND-MSG-SENDER-001-TXN-SENDER-001"
    )

    assert inbound_pacs008.idempotency_key({}) is None


# =============================================================================
# 2. the message documents
# =============================================================================

def test_the_raw_message_is_stored_before_the_payment_exists(service, db):
    """FR-1.IN1 + her L368-372. The message is persisted FIRST, with `paymentId: null`, and
    back-linked after. This ordering is the requirement, not an implementation detail."""
    service.receive_inbound(_message())

    messages = list(db["paymentMessages"].find({}))
    inbound = [m for m in messages if m["direction"] == "INBOUND"]
    assert len(inbound) == 1
    stored = inbound[0]
    # Back-linked once the payment existed.
    assert stored["paymentId"] == _payment_doc(db)["paymentId"]
    # The sender's original, kept verbatim — what makes replay and reconstruction possible.
    assert stored["rawMessage"] == _message()
    assert stored["purpose"] == inbound_documents.PURPOSE_CREDIT_TRANSFER


def test_a_message_that_fails_to_parse_is_still_stored(service, db):
    """The whole reason the insert precedes the parse (her L369). A rejected message must
    remain on disk to be inspected or replayed — losing it is the failure this ordering
    exists to prevent."""
    with pytest.raises(inbound_pacs008.MessageRejected):
        service.receive_inbound({"Document": {"NotAPacs008": {}}})

    stored = list(db["paymentMessages"].find({}))
    assert len(stored) == 1
    # Never linked, because no payment was ever created.
    assert stored[0]["paymentId"] is None
    assert list(db["payments"].find({})) == []


def test_no_inbound_message_is_a_second_canonical_payment(service, db):
    """Her L887: `payments` is the only authoritative canonical payment collection.

    Holds for all three inbound message documents, the same contract
    `test_the_payment_message_is_not_a_second_canonical_payment` enforces outbound.
    """
    service.receive_inbound(_message())

    for message in db["paymentMessages"].find({}):
        for forbidden in ("status", "balance", "lifecycle", "debtor", "creditor"):
            assert forbidden not in message, (
                f"{message['paymentMessageId']} carries {forbidden!r} — a message must "
                "never be a second canonical payment."
            )


def test_a_duplicate_message_credits_the_customer_once(service, db):
    """Her L370: duplicates are detected BEFORE a duplicate payment exists."""
    first = service.receive_inbound(_message())
    balance_after_first = db["accounts"].find_one({"accountId": CREDITOR})["balance"]["current"]

    second = service.receive_inbound(_message())

    assert second["paymentId"] == first["paymentId"]
    assert len(list(db["payments"].find({}))) == 1
    assert (
        db["accounts"].find_one({"accountId": CREDITOR})["balance"]["current"]
        == balance_after_first
    ), "a replayed message credited the customer twice"
    # Visible evidence, so the demo beat is not invisible.
    names = [c["name"] for c in _payment_doc(db)["checks"]]
    assert "inbound_duplicate_absorbed" in names


# =============================================================================
# 3. the happy path
# =============================================================================

def test_an_inbound_wire_runs_from_received_to_settled(service, db):
    """The full sequence, driven through the real saga."""
    result = service.receive_inbound(_message())
    payment = _payment_doc(db)

    assert payment["direction"] == "INBOUND"
    assert payment["rail"] == "WIRE"
    assert payment["lifecycle"]["currentState"] == "SETTLED"

    states = [e["state"] for e in payment["lifecycle"]["events"]]
    assert states == [
        "DRAFT", "RECEIVED", "VALIDATED", "ENRICHED", "FINAL_VALIDATED",
        "ACCEPTED", "IN_PROGRESS", "SETTLED",
    ], "the inbound path must skip the outbound-only ROUTED/AUTHORISED/APPROVED/SUBMITTED"
    assert result["paymentId"] == payment["paymentId"]


def test_the_credit_posts_the_mirror_legs(service, db):
    """FR-6.IN1 — `Dr Wire Clearing / Cr Customer Deposit`, the reverse of outbound FR-6.3.

    Asserted on the BALANCES and the `transactions` doc, because that doc is what the
    ledger's change stream consumes — it is the real interface to stage 6's pipeline.
    """
    before = db["accounts"].find_one({"accountId": CREDITOR})["balance"]["current"]
    clearing_before = db["accounts"].find_one(
        {"accountId": "ACC-CLEARING-WIRE"})["balance"]["current"]

    service.receive_inbound(_message())

    after = db["accounts"].find_one({"accountId": CREDITOR})["balance"]["current"]
    clearing_after = db["accounts"].find_one(
        {"accountId": "ACC-CLEARING-WIRE"})["balance"]["current"]

    assert after == before + 25_000.0, "the customer was not credited"
    assert clearing_after == clearing_before - 25_000.0, "the clearing account was not debited"

    txn = db["transactions"].find_one({})
    assert txn["direction"] == "INBOUND"
    # payer = clearing, payee = customer. The mirror, and the ONLY thing that differs from
    # an outbound posting — `posting_rules` reads each account's own gl.accountCode, so the
    # ledger needs no inbound branch at all.
    assert txn["payer"]["accountId"] == "ACC-CLEARING-WIRE"
    assert txn["payee"]["accountId"] == CREDITOR


def test_the_beneficiary_is_notified_not_the_originator(service, db):
    """An inbound payment notifies the party who RECEIVED money. The originator banks
    elsewhere and is not ours to notify."""
    service.receive_inbound(_message())

    notifications = list(db["notifications"].find({}))
    assert len(notifications) == 1
    assert notifications[0]["eventType"] == "PaymentReceived"
    assert notifications[0]["recipient"]["customerId"] == CUST_C
    assert "You received" in notifications[0]["message"]


def test_settlement_is_confirmed_on_arrival(service, db):
    """D-IN4 + FR-7.IN2 — no clearing window to await, and `expectedPosition` comes from the
    message's instructed amount rather than a routing commitment (inbound has none)."""
    service.receive_inbound(_message())

    position = db["settlementPositions"].find_one({})
    assert position is not None
    assert position["settlementStatus"] == "SETTLED"
    assert position["expectedAmount"] == 25_000.0
    assert position["clearingAccountCode"] == "1131"

    payment = _payment_doc(db)
    assert payment["lifecycle"]["settlementStatus"] == "SETTLED"
    assert payment["refs"]["settlementPositionId"] == position["settlementPositionId"]


def test_no_payment_execution_is_written_for_an_inbound_wire(service, db):
    """Her L944 — `paymentExecutions` tracks OUTBOUND rail-message generation and stays
    outbound-only by design. An inbound payment has no execution attempt: nothing was
    submitted anywhere."""
    service.receive_inbound(_message())
    assert list(db["paymentExecutions"].find({})) == []


def test_the_status_response_is_an_outbound_message_on_an_inbound_payment(service, db):
    """FR-5.IN2, and the axis distinction that makes the whole model coherent: `direction`
    on a MESSAGE is its travel, on a PAYMENT it is the money's. They disagree here, and
    neither may be derived from the other."""
    service.receive_inbound(_message())

    responses = [
        m for m in db["paymentMessages"].find({})
        if m["purpose"] == inbound_documents.PURPOSE_STATUS_RESPONSE
    ]
    assert len(responses) == 1
    assert responses[0]["direction"] == "OUTBOUND"
    assert responses[0]["messageFormat"] == "pacs.002.001.08"
    assert responses[0]["statusCode"] == pacs002.ACCEPTED
    assert _payment_doc(db)["direction"] == "INBOUND"


# =============================================================================
# 4. beneficiary resolution — all three outcomes through the real saga
# =============================================================================

def test_an_exact_name_match_proceeds(service, db):
    service.receive_inbound(_message())
    resolution = _payment_doc(db)["beneficiaryResolution"]
    assert resolution["matchOutcome"] == name_match.MATCHED
    assert resolution["matchedAccountId"] == CREDITOR
    # The claim and the record are both kept (creditor.name is overwritten on MATCHED).
    assert resolution["nameOfRecord"] == _HOLDER_NAME
    assert resolution["accountStatus"] == "ACTIVE"
    names = {c["name"]: c["result"] for c in _payment_doc(db)["checks"]}
    assert names["beneficiary_account_open"] == "PASS"
    assert names["beneficiary_name_matched"] == "PASS"


def test_a_plausible_variant_proceeds_with_a_flag(service, db):
    """Her L500 — PARTIAL proceeds, but the discrepancy is recorded.

    ⚠️ Driven through the real saga, not by calling `name_match.compare` directly: the unit
    test proves the rule works, only this proves the outcome is REACHABLE (defect
    2026-09-01 `unreachable-control`).
    """
    variant = _HOLDER_NAME.replace("Holder", "Holdur")
    service.receive_inbound(_message(creditor_name=variant))

    payment = _payment_doc(db)
    assert payment["beneficiaryResolution"]["matchOutcome"] == name_match.PARTIAL
    assert payment["lifecycle"]["currentState"] == "SETTLED", "a PARTIAL must still settle"
    # The CLAIMED name is preserved, not overwritten — erasing it would erase the very
    # discrepancy the flag exists to report.
    assert payment["creditor"]["name"] == variant
    flagged = [c for c in payment["checks"] if c["name"] == "beneficiary_name_matched"]
    assert flagged and flagged[0]["result"] == "WARN"


def test_an_unrelated_name_becomes_an_unable_to_apply(service, db):
    """FR-2.IN4 — a NO_MATCH routes to stage 9, and does NOT reject.

    The money has already arrived; rejecting would close the payment with the funds
    stranded and no operator path to return them.
    """
    service.receive_inbound(_message(creditor_name="Completely Different Person"))

    payment = _payment_doc(db)
    assert payment["beneficiaryResolution"]["matchOutcome"] == name_match.NO_MATCH
    # HELD, not terminal — both operator actions must stay available.
    assert payment["lifecycle"]["currentState"] == "RECEIVED"
    assert payment["status"] != "REJECTED"

    exception = db["exceptions"].find_one({})
    assert exception["category"] == exc_module.CATEGORY_UTA
    assert exception["status"] == exc_module.STATUS_OPEN
    # The operator sees what was claimed without opening the message (her L1341).
    assert exception["detail"]["claimedName"] == "Completely Different Person"
    assert payment["beneficiaryResolution"]["claimedName"] == "Completely Different Person"
    assert payment["beneficiaryResolution"]["nameOfRecord"] == _HOLDER_NAME
    results = {c["name"]: c["result"] for c in payment["checks"]}
    assert results["beneficiary_account_open"] == "PASS"
    assert results["beneficiary_name_matched"] == "FAIL"


def test_an_unknown_account_becomes_an_unable_to_apply(service, db):
    service.receive_inbound(_message(iban="GB00NOSUCH0000000000"))

    payment = _payment_doc(db)
    assert payment["beneficiaryResolution"]["matchOutcome"] == name_match.NO_MATCH
    assert db["exceptions"].find_one({})["category"] == exc_module.CATEGORY_UTA


def test_a_closed_account_becomes_an_unable_to_apply(service, db):
    db["accounts"].update_one({"accountId": CREDITOR}, {"$set": {"status": "CLOSED"}})
    service.receive_inbound(_message())

    assert db["exceptions"].find_one({})["category"] == exc_module.CATEGORY_UTA
    assert _payment_doc(db)["lifecycle"]["currentState"] == "RECEIVED"


def test_no_money_moves_for_a_payment_that_cannot_be_applied(service, db):
    """The invariant behind every UTA case: the funds stay in clearing until someone
    decides where they go."""
    before = db["accounts"].find_one({"accountId": CREDITOR})["balance"]["current"]
    service.receive_inbound(_message(creditor_name="Completely Different Person"))
    after = db["accounts"].find_one({"accountId": CREDITOR})["balance"]["current"]

    assert after == before
    assert list(db["transactions"].find({})) == []


# =============================================================================
# 5. screening and the acceptance decision
# =============================================================================

def test_a_sanctioned_originator_is_refused_despite_a_matched_beneficiary(service, db):
    """FR-4.IN1 and her L831's exact case: a sanctions hit *"despite a matched beneficiary"*
    still rejects. Both inputs must be acceptable, not either."""
    service.receive_inbound(_message(debtor_name="Vostok Heavy Industries"))

    payment = _payment_doc(db)
    assert payment["beneficiaryResolution"]["matchOutcome"] == name_match.MATCHED
    assert payment["acceptanceDecision"]["decision"] == "REJECT"
    assert payment["acceptanceDecision"]["reasonCode"] == "RR04"
    assert payment["correspondent"]["sanctionsCheck"]["status"] == "HIT"
    # FR-4.IN3 — not advanced to stage 5/6, routed to stage 9.
    assert payment["lifecycle"]["currentState"] == "FINAL_VALIDATED"
    assert db["exceptions"].find_one({})["category"] == exc_module.CATEGORY_UTA


def test_a_restricted_origin_country_is_refused(service, db):
    """The originator's country comes off the BIC when the party carries none — chars 5-6,
    ISO 9362. Without that, screening would never fire on the field most messages populate."""
    service.receive_inbound(_message(debtor_bic="BANKIRTH"))  # IR = restricted

    payment = _payment_doc(db)
    assert payment["correspondent"]["sanctionsCheck"]["status"] == "BLOCKED"
    assert payment["acceptanceDecision"]["decision"] == "REJECT"


def test_a_clean_payment_is_accepted_with_both_inputs_recorded(service, db):
    """Her demo panel (L835) prints the decision WITH its reasons, so both inputs are stored
    alongside the roll-up rather than being recoverable only from other fields."""
    service.receive_inbound(_message())

    decision = _payment_doc(db)["acceptanceDecision"]
    assert decision["decision"] == "ACCEPT"
    assert decision["reasonCode"] is None
    assert decision["beneficiaryMatch"] == name_match.MATCHED
    assert decision["sanctionsStatus"] == "CLEAR"


def test_the_corridor_is_classified_from_the_originator_not_the_beneficiary(service, db):
    """FR-3.IN3 — the same logic as outbound FR-3.7 with the comparison inputs swapped."""
    cross_border = service.receive_inbound(_message())
    # A US originator (chars 5-6 of the BIC) makes the same corridor domestic. Both run in
    # one db, so each is looked up by its own paymentId rather than clearing the collection.
    domestic = service.receive_inbound(_message(debtor_bic="CHASUS33", uetr="UETR-domestic"))

    assert _payment_doc(db, cross_border["paymentId"])["validation"][
        "determinedCategory"] == "CROSS_BORDER"
    assert _payment_doc(db, domestic["paymentId"])["validation"][
        "determinedCategory"] == "DOMESTIC"


# =============================================================================
# 6. Unable to Apply — Repair and Return
# =============================================================================

def _held(service, db, **over):
    """Drive a payment into the UTA queue and return `(payment, exception)`."""
    over.setdefault("creditor_name", "Completely Different Person")
    service.receive_inbound(_message(**over))
    return _payment_doc(db), db["exceptions"].find_one({})


def test_repair_persists_the_correction_before_resuming(service, db):
    """FR-9.IN2 Repair, and the reason the persistence is separate from the resume.

    ⚠️ This is the defect-2026-09-28 (`control-not-persisted-across-reentry`) guard. The
    resume rebuilds its context FROM THE DOCUMENT, so a correction held only in memory
    would silently un-apply. Asserting the stored `beneficiaryResolution` — not just the
    end state — is what makes that failure visible.
    """
    payment, exception = _held(service, db)
    from contexts.financial_gateway.application import uta

    uta.repair(service, exception, payment, matched_account_id=CREDITOR,
               note="Confirmed with the sending bank by phone.")

    repaired = _payment_doc(db)
    resolution = repaired["beneficiaryResolution"]
    assert resolution["matchOutcome"] == name_match.MATCHED
    assert resolution["matchedAccountId"] == CREDITOR
    # NEVER `EXACT`: a human confirming a beneficiary is different evidence from an
    # algorithm matching a string, and an auditor must be able to tell which happened.
    assert resolution["matchMethod"] == name_match.METHOD_MANUAL_REPAIR
    assert resolution["repairedBy"] == "payments-operations"


def test_a_repaired_payment_resumes_and_credits_the_customer(service, db):
    """Her L1336 — the payment resumes at stage 3 and proceeds to credit posting."""
    payment, exception = _held(service, db)
    before = db["accounts"].find_one({"accountId": CREDITOR})["balance"]["current"]
    from contexts.financial_gateway.application import uta

    uta.repair(service, exception, payment, matched_account_id=CREDITOR)

    repaired = _payment_doc(db)
    assert repaired["lifecycle"]["currentState"] == "SETTLED"
    assert (
        db["accounts"].find_one({"accountId": CREDITOR})["balance"]["current"]
        == before + 25_000.0
    )


def test_repair_refuses_an_account_that_cannot_be_credited(service, db):
    """The precondition runs BEFORE the claim (defect 2026-09-28 A4), so a refusal leaves
    the exception open and retryable rather than silently closed."""
    payment, exception = _held(service, db)
    db["accounts"].update_one({"accountId": CREDITOR}, {"$set": {"status": "CLOSED"}})
    from contexts.financial_gateway.application import uta

    with pytest.raises(ValueError, match="not ACTIVE"):
        uta.repair(service, exception, payment, matched_account_id=CREDITOR)

    assert db["exceptions"].find_one({})["status"] == exc_module.STATUS_OPEN
    assert _payment_doc(db)["lifecycle"]["currentState"] == "RECEIVED"


def test_return_generates_a_pacs004_and_closes_the_payment(service, db):
    """FR-9.IN3 — the return message, and no customer credited."""
    payment, _ = _held(service, db)
    before = db["accounts"].find_one({"accountId": CREDITOR})["balance"]["current"]
    from contexts.financial_gateway.application import uta

    uta.build_return(service, payment, return_reason_code=pacs004.REASON_ACCOUNT_INCORRECT)

    returns = [
        m for m in db["paymentMessages"].find({})
        if m["purpose"] == inbound_documents.PURPOSE_RETURN
    ]
    assert len(returns) == 1
    assert returns[0]["direction"] == "OUTBOUND"
    assert returns[0]["messageFormat"] == "pacs.004.001.09"
    # No Leafy Bank customer is credited (her L1337).
    assert db["accounts"].find_one({"accountId": CREDITOR})["balance"]["current"] == before


def test_a_return_before_acceptance_closes_rejected_not_returned(service, db):
    """DR-9.IN2's distinction, which is a real one: REJECTED means never accepted,
    RETURNED means accepted and then returned. A payment held at stage 2 was never
    accepted — nothing was ever promised to the sender."""
    payment, _ = _held(service, db)
    from contexts.financial_gateway.application import uta

    uta.build_return(service, payment, return_reason_code=pacs004.REASON_ACCOUNT_INCORRECT)

    assert _payment_doc(db)["lifecycle"]["currentState"] == "REJECTED"


def test_the_uta_queue_never_opens_two_rows_for_one_payment(service, db):
    """Same dedupe key and index as the outbound writer — a replay cannot double-queue."""
    service.receive_inbound(_message(creditor_name="Completely Different Person"))
    service.receive_inbound(_message(creditor_name="Completely Different Person"))

    assert len(list(db["exceptions"].find({}))) == 1


# =============================================================================
# 7. the generated messages — element NAMES, not just values
# =============================================================================
#
# ⚠️ Defect 2026-09-02 (`message-structure-unsourced`): the outbound pacs.008 shipped three
# ISO-structure errors, and its purpose-built guard could not see any of them because that
# guard checked value PROVENANCE. A value-provenance guard is structurally blind to a
# well-named-looking element that should not exist. These check names.

def test_the_pacs002_has_the_iso_envelope_and_root():
    message = pacs002.build(payment={"paymentId": "PAY-1"}, accepted=True, now=NOW)
    assert "Document" in message
    assert "FIToFIPmtStsRpt" in message["Document"]


def test_the_pacs004_has_the_iso_envelope_and_root():
    message = pacs004.build(payment={"paymentId": "PAY-1"}, return_reason_code="AC01", now=NOW)
    assert "Document" in message
    assert "PmtRtr" in message["Document"]


@pytest.mark.parametrize("builder,body_fn,repeating", [
    (lambda: pacs002.build(payment={"paymentId": "P"}, accepted=True, now=NOW),
     pacs002.body, "TxInfAndSts"),
    (lambda: pacs004.build(payment={"paymentId": "P"}, return_reason_code="AC01", now=NOW),
     pacs004.body, "TxInf"),
])
def test_the_repeating_element_is_a_list(builder, body_fn, repeating):
    """1..n in ISO. A single object would be a different document shape from every real
    message and would break any consumer that iterates it — the exact bug the pacs.008
    shipped on 2026-09-02, not repeated here."""
    assert isinstance(body_fn(builder())[repeating], list)


@pytest.mark.parametrize("builder", [
    lambda: pacs002.build(payment={"paymentId": "P"}, accepted=True, now=NOW),
    lambda: pacs004.build(payment={"paymentId": "P"}, return_reason_code="AC01", now=NOW),
])
def test_no_element_from_a_neighbouring_message_type_appears(builder):
    """The check that would have caught the 2026-09-02 `PmtMtd` bug.

    `PmtMtd` is pain.001 (an initiation element) and appears in no pacs message at all.
    `IntrBkSttlmAmt` / `ChrgBr` / `Dbtr` / `Cdtr` belong to pacs.008 — a status report and a
    return carry references and their OWN amount element (`RtrdIntrBkSttlmAmt`), never the
    original instruction's.
    """
    import json

    serialised = json.dumps(builder(), default=str)
    for foreign in ('"PmtMtd"', '"IntrBkSttlmAmt"', '"ChrgBr"', '"CdtTrfTxInf"'):
        assert foreign not in serialised, (
            f"{foreign} belongs to a neighbouring message type and must not appear here."
        )


def test_the_status_report_quotes_the_senders_references_not_ours():
    """A report quoting our own identifiers would be unmatchable by the bank that has to
    reconcile it against what they sent."""
    payment = {
        "paymentId": "PAY-local",
        "uetr": "UETR-x",
        "senderReferences": {"msgId": "THEIR-MSG", "endToEndId": "THEIR-E2E",
                             "txId": "THEIR-TX", "instructionId": "THEIR-INSTR"},
        "debtor": {"bic": "DEUTDEFF", "bankName": "Deutsche"},
    }
    body = pacs002.body(pacs002.build(payment=payment, accepted=True, now=NOW))

    assert body["OrgnlGrpInfAndSts"]["OrgnlMsgId"] == "THEIR-MSG"
    status = body["TxInfAndSts"][0]
    assert status["OrgnlInstrId"] == "THEIR-INSTR"
    assert status["OrgnlEndToEndId"] == "THEIR-E2E"
    assert status["OrgnlTxId"] == "THEIR-TX"
    # The UETR travels unchanged across every hop — the point of it. The stored `UETR-`
    # prefix is internal; ISO schema validation wants the bare value.
    assert status["OrgnlUETR"] == "x"
    assert "OrgnlTxRef" not in status and "OrgnlTxRef" not in str(body["OrgnlGrpInfAndSts"])
    assert "MsgRef" not in str(body)
    # Our own reference is still present, as the account-servicer reference.
    assert status["AcctSvcrRef"] == "PAY-local"


def test_iso_uetr_strips_the_storage_prefix_and_is_idempotent():
    uuid = "97ad5f26-ec2a-48fc-b4b2-5583b16aac27"
    assert pacs008.iso_uetr(f"UETR-{uuid}") == uuid
    assert pacs008.iso_uetr(uuid) == uuid
    assert pacs008.iso_uetr(None) is None


def test_the_return_carries_a_bare_uetr():
    payment = {"paymentId": "PAY-1", "uetr": "UETR-abc"}
    transaction = pacs004.body(
        pacs004.build(payment=payment, return_reason_code="AC01", now=NOW)
    )["TxInf"][0]
    assert transaction["OrgnlUETR"] == "abc"


def test_a_refusal_carries_a_reason_and_an_acceptance_does_not():
    accepted = pacs002.body(pacs002.build(payment={}, accepted=True, now=NOW))
    assert accepted["TxInfAndSts"][0]["TxSts"] == pacs002.ACCEPTED
    assert accepted["TxInfAndSts"][0]["StsRsnInf"] is None

    refused = pacs002.body(
        pacs002.build(payment={}, accepted=False, reason_code="RR04", now=NOW)
    )
    assert refused["TxInfAndSts"][0]["TxSts"] == pacs002.REJECTED
    assert refused["TxInfAndSts"][0]["StsRsnInf"]["Rsn"]["Cd"] == "RR04"
    # The coded reason carries its plain-English explanation, and a refusal was never accepted.
    assert refused["TxInfAndSts"][0]["StsRsnInf"]["AddtlInf"] == "Sanctions screening hit on originator"
    assert refused["TxInfAndSts"][0]["AccptncDtTm"] is None
    # An acceptance carries the acceptance time and the STSRPT message id, and no reason.
    assert accepted["TxInfAndSts"][0]["AccptncDtTm"] == NOW
    assert accepted["TxInfAndSts"][0]["StsRsnInf"] is None
    named = pacs002.body(pacs002.build(payment={"paymentId": "PAY-1bcf3cc0"}, accepted=True, now=NOW))
    assert named["GrpHdr"]["MsgId"] == f"STSRPT-{NOW:%Y%m%d}-1BCF3CC0"


def test_the_return_swaps_the_parties():
    """The semantic of a return: the bank that received the funds is now sending them."""
    payment = {
        "paymentId": "PAY-1",
        "debtor": {"name": "Acme GmbH", "bic": "DEUTDEFF", "bankName": "Deutsche"},
        "creditor": {"name": "Frida Nilsen"},
        "instructedAmount": 25_000.0, "instructedCurrency": "USD",
    }
    transaction = pacs004.body(
        pacs004.build(payment=payment, return_reason_code="AC01", now=NOW)
    )["TxInf"][0]

    chain = transaction["RtrChain"]
    assert chain["Dbtr"]["Pty"]["Nm"] == "Frida Nilsen", "we are now the sender"
    assert chain["Cdtr"]["Pty"]["Nm"] == "Acme GmbH", "the originator now receives"
    # `RtrdIntrBkSttlmAmt`, not `IntrBkSttlmAmt` — a distinct element because a return may
    # be partial.
    assert transaction["RtrdIntrBkSttlmAmt"][pacs008.TEXT_KEY] == 25_000.0


@pytest.mark.parametrize("to_xml,root", [
    (lambda: pacs002.to_xml(pacs002.build(payment={"paymentId": "P"}, accepted=True, now=NOW)),
     "FIToFIPmtStsRpt"),
    (lambda: pacs004.to_xml(
        pacs004.build(payment={"paymentId": "P"}, return_reason_code="AC01", now=NOW)),
     "PmtRtr"),
])
def test_each_message_serialises_to_well_formed_xml(to_xml, root):
    """⚠️ Well-formed and in the right shape; NOT XSD-validated — no ISO schema is available
    in this workspace. Same honest limit as the pacs.008's serialiser test."""
    from xml.etree import ElementTree as ET

    xml = to_xml()
    parsed = ET.fromstring(xml)
    # ElementTree resolves the default `xmlns` onto every tag as `{namespace}Tag`, so the
    # comparison strips it. The namespace being PRESENT is correct — an ISO message without
    # one is not a valid document — which is why this asserts on the suffix rather than
    # dropping the declaration to make the test simpler.
    assert parsed.tag.rsplit("}", 1)[-1] == "Document"
    assert parsed[0].tag.rsplit("}", 1)[-1] == root


# =============================================================================
# 8. the service-level resolve route (the operator's actual entry point)
# =============================================================================

def test_resolve_uta_repairs_through_the_service(service, db):
    """The path the UI takes: exception id + account, no direct module call."""
    payment, exception = _held(service, db)

    updated = service.resolve_uta(
        exception["exceptionId"], action="REPAIR", matched_account_id=CREDITOR,
        note="Confirmed by phone.",
    )

    assert updated["status"] == exc_module.STATUS_RESOLVED
    assert updated["resolution"]["action"] == "REPAIR"
    assert _payment_doc(db)["lifecycle"]["currentState"] == "SETTLED"


def test_resolve_uta_returns_through_the_service(service, db):
    payment, exception = _held(service, db)

    updated = service.resolve_uta(
        exception["exceptionId"], action="RETURN",
        return_reason_code=pacs004.REASON_ACCOUNT_INCORRECT,
    )

    assert updated["status"] == exc_module.STATUS_RESOLVED
    assert updated["resolution"]["action"] == "RETURN"
    assert _payment_doc(db)["lifecycle"]["currentState"] == "REJECTED"


def test_a_failed_repair_leaves_the_exception_open(service, db):
    """Defect 2026-09-28 A4 met by construction: the work runs BEFORE the claim, so a
    failure cannot leave a silently-closed row the operator can no longer see."""
    payment, exception = _held(service, db)

    with pytest.raises(ValueError):
        service.resolve_uta(
            exception["exceptionId"], action="REPAIR",
            matched_account_id="ACC-does-not-exist",
        )

    assert db["exceptions"].find_one({})["status"] == exc_module.STATUS_OPEN


def test_uta_actions_are_refused_on_an_outbound_exception(service, db):
    """REPAIR/RETURN apply only to an Unable-to-Apply exception — the mirror of
    `resolve_exception` refusing UTA-only actions on the outbound categories."""
    _held(service, db)
    db["exceptions"].update_one(
        {}, {"$set": {"category": exc_module.CATEGORY_SETTLEMENT_DELAYED}}
    )
    exception = db["exceptions"].find_one({})

    with pytest.raises(ValueError, match="not legal"):
        service.resolve_uta(
            exception["exceptionId"], action="REPAIR", matched_account_id=CREDITOR,
        )


# =============================================================================
# 9. the simulator — the background + manual inbound trigger
# =============================================================================

def test_every_scenario_builds_a_message_the_real_parser_accepts():
    """The simulator must feed the REAL pipeline, not a shape it invented. Every scenario
    builds a parseable pacs.008, and its mutation classifies as the scenario claims —
    grading the generator against the consumer, not against itself."""
    from contexts.financial_gateway.domain import simulate

    for scenario in ("HAPPY", "PARTIAL", "MISMATCH", "SANCTIONS", "FX"):
        message = simulate.build_message(
            scenario=scenario,
            beneficiary_name=_HOLDER_NAME,
            beneficiary_identifier=_IBAN,
            identifier_is_iban=True,
            amount=5_000.0,
        )
        parsed = inbound_pacs008.parse(message)
        assert parsed["amount"] == 5_000.0
        assert parsed["claimedCreditor"]["iban"] == _IBAN

        if scenario == "HAPPY":
            assert parsed["claimedCreditor"]["name"] == _HOLDER_NAME
        elif scenario == "PARTIAL":
            outcome, _ = name_match.compare(
                parsed["claimedCreditor"]["name"], _HOLDER_NAME
            )
            assert outcome == name_match.PARTIAL
        elif scenario == "MISMATCH":
            outcome, _ = name_match.compare(
                parsed["claimedCreditor"]["name"], _HOLDER_NAME
            )
            assert outcome == name_match.NO_MATCH
        elif scenario == "SANCTIONS":
            assert parsed["debtor"]["name"] in {
                name for name in __import__(
                    "contexts.fraud_evaluation.domain.sanctions", fromlist=["denied_parties"]
                ).denied_parties()
            }
        elif scenario == "FX":
            assert parsed["currency"] == "EUR"  # the account's is USD


def test_one_ambient_cycle_is_exactly_two_happy_payments_and_nothing_else(service, db):
    """Kiran (2026-09-29, revised same day): one ambient cycle = EXACTLY TWO happy-path
    inbound payments. Happy path only — ambient traffic NEVER opens exceptions and never
    fires an agent; an unattended simulator queuing operator work is a queue that fills
    itself. The holding/refusing variants (MISMATCH, SANCTIONS) are manual-trigger-only,
    where a human chose to create the work. This pins the count (a number, not a draw) and
    the scenario together, through the worker's own `generate_cycle`."""
    from workers import inbound_sim_worker

    before = len(list(db["payments"].find({})))
    payments = inbound_sim_worker.generate_cycle(service)

    assert len(payments) == inbound_sim_worker.PAYMENTS_PER_CYCLE == 2
    assert len(list(db["payments"].find({}))) == before + 2
    for payment in payments:
        assert payment["lifecycle"]["currentState"] == "SETTLED", (
            "an ambient payment must settle — it has no operator to un-hold it"
        )
    assert list(db["exceptions"].find({})) == []


@pytest.mark.parametrize("scenario,expected_state,expected_resolution", [
    ("HAPPY", "SETTLED", name_match.MATCHED),
    ("PARTIAL", "SETTLED", name_match.PARTIAL),
    ("MISMATCH", "RECEIVED", name_match.NO_MATCH),
    ("SANCTIONS", "FINAL_VALIDATED", name_match.MATCHED),
])
def test_each_scenario_runs_through_the_service_to_its_named_outcome(
        service, db, scenario, expected_state, expected_resolution):
    """The manual trigger's real path, one scenario at a time, beneficiary pinned — the
    account draw is right for a demo trigger and wrong for an assertion."""
    payment = service.simulate_inbound(scenario, account_id=CREDITOR)

    assert payment["direction"] == "INBOUND"
    assert payment["lifecycle"]["currentState"] == expected_state
    assert payment["beneficiaryResolution"]["matchOutcome"] == expected_resolution
    if scenario == "SANCTIONS":
        assert payment["acceptanceDecision"]["decision"] == "REJECT"
        assert db["exceptions"].find_one({})["category"] == exc_module.CATEGORY_UTA
    if scenario == "MISMATCH":
        assert db["exceptions"].find_one({})["category"] == exc_module.CATEGORY_UTA


def test_the_fx_scenario_converts_before_crediting(service, db):
    """EUR instructed into a USD account: the credited amount is converted, the instructed
    amount is preserved, and the `fx{}` object records that the rate is SIMULATED."""
    from contexts.financial_gateway.domain import simulate as sim

    payment = service.simulate_inbound("FX", account_id=CREDITOR)

    instructed = payment["instructedAmount"]
    assert payment["instructedCurrency"] == "EUR"
    assert payment["currency"] == "USD"
    assert payment["amount"] == round(instructed * 1.0850, 2)
    assert payment["fx"]["fxRate"] == 1.0850
    assert payment["fx"]["rateSource"] == "SIMULATED-FX-v1"
    # The customer was credited the CONVERTED amount, in their own currency.
    txn = db["transactions"].find_one({})
    assert txn["amount"] == payment["amount"]
    assert txn["currency"] == "USD"


def test_the_duplicate_scenario_is_one_payment_not_two(service, db):
    """The same message sent twice is one payment and one credit — the demo beat the
    DUPLICATE scenario exists for, asserted on the balance rather than on an id."""
    before = db["accounts"].find_one({"accountId": CREDITOR})["balance"]["current"]

    first = service.simulate_inbound("DUPLICATE", account_id=CREDITOR)

    after = db["accounts"].find_one({"accountId": CREDITOR})["balance"]["current"]
    assert first["status"] == "SETTLED"
    assert after == before + first["amount"], "the replay credited the customer twice"
    # Both messages are stored (audit), but exactly one payment exists.
    inbound_messages = [
        m for m in db["paymentMessages"].find({})
        if m["direction"] == "INBOUND"
    ]
    assert len(inbound_messages) == 2
    assert len(list(db["payments"].find({}))) == 1


def test_an_unknown_scenario_is_refused_at_the_service(service, db):
    with pytest.raises(ValueError, match="Unknown inbound scenario"):
        service.simulate_inbound("NOT_A_SCENARIO", account_id=CREDITOR)


def test_the_simulator_only_names_customer_accounts_as_beneficiaries(service, db):
    """The shared `accounts` collection holds NOSTRO clearing accounts too — a simulated
    wire whose beneficiary IS the clearing account would corrupt the mirror posting. The
    simulator's candidate query must exclude every bank-internal type (defect 2026-06-29,
    read-side edition). Asserted by pinning each in turn: only a customer account works."""
    # Seed a NOSTRO with an iban and a named holder, so the ONLY thing that can exclude
    # it is the type filter. Drop every other account so the draw MUST consider it.
    db["accounts"].docs = [
        {**_account("ACC-NOSTRO-TRAP", "CUST-CUSTOMERONLY"), "type": "NOSTRO"}
    ]
    with pytest.raises(ValueError, match="No active customer account"):
        service.simulate_inbound("HAPPY")


def test_the_settled_inbound_payment_carries_the_fields_the_settlement_worker_needs(service, db):
    """The live 2026-09-29 crash-loop, pinned.

    `settlement_worker` (CDC on `lifecycle.settlementStatus == SETTLED`) requires
    `clearing.settlementAccountCode` — outbound `settle.py` stamps it at the SETTLED
    transition. The first inbound build stamped no such field, so the worker raised on the
    payment and crash-looped, blocking every settlement queued behind it (the 2026-07-01
    incident class, reproduced by the incoming flow). An inbound payment that settles
    WITHOUT this field is a poisoned payment on a live cluster — this test is the guard
    that says the stamping happened.
    """
    service.simulate_inbound("HAPPY", account_id=CREDITOR)

    payment = _payment_doc(db)
    assert payment["lifecycle"]["settlementStatus"] == "SETTLED"
    code = payment["clearing"]["settlementAccountCode"]
    assert code, "a SETTLED inbound payment must carry clearing.settlementAccountCode"
    # From settle.py's CORRESPONDENT model row — the inbound story — not restated here.
    from contexts.payment_settlement import settle as settle_mod
    assert code == settle_mod._SETTLEMENT_MODELS["CORRESPONDENT"]["settlementAccountCode"]

    # And the position agrees: same model, same code — no incoherent pair on one row.
    position = db["settlementPositions"].find_one({})
    assert position["settlementAccountCode"] == code
    assert position["clearingAccountCode"] == settle_mod._WIRE_CLEARING_CODE
