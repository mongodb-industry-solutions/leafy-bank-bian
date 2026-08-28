"""T1 — spec conformance for the built `payments` document (doc 13 §2 B4, §4).

Why this file exists instead of the collection validator: applying
`propose_payments.json`'s `$jsonSchema` to the collection today rejects EVERY insert, for
three reasons stage 1 cannot fix — `fraud` is `required` and non-nullable but is written in
stage 4b (Doina Q9), amounts must be `Decimal128`, and timestamps must be ISO strings. The
latter two are a repo-wide type migration. So the loud-failure benefit is taken here
instead: load the canonical spec and assert the document's required keys, enum membership
and block shapes against it.

This also discharges the enum-drift defect's own prevention rule (umbrella defects.md,
2026-04-28): *"add a drift test that walks every field and asserts its name + enum values
match the canonical spec"*.

The two known deviations are asserted as deviations at the bottom, so the day the type
migration lands, this file fails and tells you to finish the job.
"""

import json
import pathlib
from datetime import date, datetime, timezone

import pytest
from bson import ObjectId

from contexts.payment_order_initiation.domain import payment_document
from process.payment_context import PaymentContext

_SPEC_PATH = (
    pathlib.Path(__file__).resolve().parents[4] / "doinas-research" / "propose_payments.json"
)


@pytest.fixture(scope="module")
def schema():
    spec = json.loads(_SPEC_PATH.read_text())
    return spec["collections"]["payments"]["validator"]["$and"][0]["$jsonSchema"]


def _ctx(**over):
    """A captured context, as `capture.py` leaves it — no database involved."""
    account = {"accountId": "ACC-debtor01", "accountNumber": "828299301",
               "type": "CHECKING", "currency": "USD"}
    customer = {
        "customerId": "CUST-0000000001",
        "identification": {"legalName": "Ada Lovelace"},
        "contact": {"addresses": [{"line1": "1 Analytical Way", "city": "London",
                                   "country": "GB"}]},
    }
    creditor_account = dict(account, accountId="ACC-credit01", accountNumber="828299302")
    creditor_customer = dict(customer, customerId="CUST-0000000002")

    kwargs = dict(
        customer_ref="CUST-0000000001",
        debtor_account_ref="ACC-debtor01",
        creditor_account_ref="ACC-credit01",
        instructed_amount=250.0,
        instructed_currency="USD",
        payment_type="CREDIT_TRANSFER",
        payment_rail="INTERNAL",
        requested_execution_date=date(2026, 8, 28),
    )
    kwargs.update(over)
    ctx = PaymentContext(**kwargs)
    ctx.now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    ctx.payment_oid = ObjectId("661a4e0583b3a4567890abcd")
    ctx.payment_id = "PAY-7890abcd"
    ctx.end_to_end_id = "E2E-567890abcd"
    ctx.debtor_account = account
    ctx.debtor_customer = customer
    ctx.debtor_customer_id = customer["customerId"]
    if not ctx.is_external_creditor and ctx.creditor_account_ref:
        ctx.creditor_account = creditor_account
        ctx.creditor_customer = creditor_customer
        ctx.creditor_customer_id = creditor_customer["customerId"]
    return ctx


def _external_ctx(**over):
    """Doina's flagship `wire_domestic` shape — an external creditor, no account."""
    ctx = _ctx(creditor_account_ref=None, payment_rail="WIRE", **over)
    ctx.is_external_creditor = True
    ctx.creditor_party = {
        "accountNo": "9876543210",
        "name": "Acme Corp",
        "bic": "CHASUS33",
        "bankName": "JPMorgan Chase",
        "bankCountry": "US",
    }
    return ctx


# --- the gate: required keys and enum membership ------------------------------

@pytest.mark.parametrize("rail", ["INTERNAL", "WIRE", "ACH", "CARD", "RTP"])
def test_every_required_field_is_written(schema, rail):
    doc = payment_document.build(_ctx(payment_rail=rail))
    missing = [k for k in schema["required"] if k not in doc]
    assert not missing, f"required fields absent from the built document: {missing}"


def test_no_field_is_written_that_the_spec_does_not_declare(schema):
    """`isInternal` / `createdBy` are the two known extras and are legal (the spec sets no
    `additionalProperties: false`). Pinned so a THIRD one can't appear unnoticed."""
    doc = payment_document.build(_ctx())
    extras = set(doc) - set(schema["properties"])
    assert extras == {"isInternal", "createdBy"}


def _enum_fields(schema):
    """Every (dotted path, allowed values) pair the spec declares, one level deep."""
    for name, prop in schema["properties"].items():
        if "enum" in prop:
            yield name, prop["enum"]
        for sub, subprop in (prop.get("properties") or {}).items():
            if "enum" in subprop:
                yield f"{name}.{sub}", subprop["enum"]


def _dig(doc, dotted):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


@pytest.mark.parametrize("rail", ["INTERNAL", "WIRE", "ACH", "CARD", "RTP"])
def test_every_written_enum_value_is_legal(schema, rail):
    doc = payment_document.build(_ctx(payment_rail=rail))
    for path, allowed in _enum_fields(schema):
        value = _dig(doc, path)
        if value is None and None not in allowed:
            continue  # absent/unwritten is a required-field concern, tested above
        assert value in allowed, f"{path}={value!r} is not in the spec enum {allowed}"


@pytest.mark.parametrize(
    "block",
    ["debtor", "creditor", "remittance", "correspondent", "cardTxn", "rtp", "clearing",
     "refs", "initiation", "wireDetails", "achDetails", "internalDetails"],
)
def test_object_block_shapes_match_the_spec(schema, block):
    """Sub-document shape, not just top-level keys — the 2026-05-04 `sub-doc-shape-skipped`
    defect was exactly this gap."""
    doc = payment_document.build(_ctx())
    assert set(doc[block]) == set(schema["properties"][block]["properties"]), block


def test_blocks_the_spec_requires_non_null_are_objects_not_none(schema):
    """`cardTxn` and `rtp` are required OBJECTS. Writing None satisfies "key present" and
    nothing else, and `rtp.isRFP` is a required non-nullable bool."""
    doc = payment_document.build(_ctx())
    assert isinstance(doc["cardTxn"], dict)
    assert isinstance(doc["rtp"], dict)
    assert doc["rtp"]["isRFP"] is False
    assert doc["correspondent"]["regulatoryReports"] == []


# --- stage-1 behaviour the shape alone would not catch ------------------------

def test_payment_type_comes_from_the_request_not_a_hardcode():
    """R3 — the customer's selection, set at initiation and never inferred later."""
    assert payment_document.build(_ctx(payment_type="RTP", payment_rail="RTP"))["type"] == "RTP"


def test_requested_execution_date_is_an_iso_date_string():
    doc = payment_document.build(_ctx(requested_execution_date=date(2026, 8, 28)))
    assert doc["requestedExecutionDate"] == "2026-08-28"


def test_end_to_end_id_and_idempotency_key_are_separate_fields():
    """R7 — the caller's retry key must not become the payment's public identity."""
    doc = payment_document.build(_ctx(idempotency_key="caller-retry-key-1"))
    assert doc["idempotencyKey"] == "caller-retry-key-1"
    assert doc["endToEndId"] == "E2E-567890abcd"


def test_stage_four_and_five_timestamps_are_not_stamped_at_creation():
    """`authorisedAt` is stage 4's and `submittedAt` is stage 5's. Stamping all four at
    creation made the demo's timeline a fiction."""
    clearing = payment_document.build(_ctx())["clearing"]
    assert clearing["receivedAt"] is not None
    assert clearing["validatedAt"] is not None
    assert clearing["authorisedAt"] is None
    assert clearing["submittedAt"] is None
    assert clearing["settledAt"] is None


def test_debtor_snapshot_carries_our_agent_identity_and_a_string_address():
    debtor = payment_document.build(_ctx())["debtor"]
    assert debtor["bic"] == "LEAFUS33"
    assert debtor["bankName"] == "Leafy Bank"
    assert debtor["bankCountry"] == "US"
    assert debtor["address"] == "1 Analytical Way, London, GB", "spec types address as a string"
    assert debtor["accountType"] == "Checking", "title-case, per the shared enum"
    assert debtor["clearingSystemMemberId"] is None, "D10 — stage 3 resolves it"


def test_external_creditor_is_snapshotted_from_the_request():
    """The `wire_domestic` scenario: no Leafy Bank account, so no lookup to snapshot."""
    doc = payment_document.build(_external_ctx())
    assert doc["creditor"]["accountId"] is None
    assert doc["creditor"]["accountNo"] == "9876543210"
    assert doc["creditor"]["name"] == "Acme Corp"
    assert doc["creditor"]["bic"] == "CHASUS33"
    assert doc["wireDetails"]["wireType"] == "DOMESTIC", "US debtor bank, US creditor bank"
    assert doc["wireDetails"]["paymentInformationId"] == "PMTINF-7890abcd"


def test_refs_block_is_written_all_null():
    """D3 — forward pointers exist so later stages can $set without an $exists branch."""
    refs = payment_document.build(_ctx())["refs"]
    assert refs["paymentExecutionIds"] == []
    assert all(v is None for k, v in refs.items() if k != "paymentExecutionIds")


# --- the two deviations we are knowingly carrying (doc 13 §2 B4) --------------

def test_known_deviation_amounts_are_floats_not_decimal128(schema):
    """When this fails, the Decimal128 migration has landed — apply the collection
    validator and delete this test."""
    assert schema["properties"]["amount"]["bsonType"] == "decimal"
    assert isinstance(payment_document.build(_ctx())["amount"], float)


def test_known_deviation_timestamps_are_datetimes_not_iso_strings(schema):
    """Ditto for the ISO-8601 string migration."""
    assert schema["properties"]["initiatedAt"]["bsonType"] == "string"
    assert isinstance(payment_document.build(_ctx())["initiatedAt"], datetime)
