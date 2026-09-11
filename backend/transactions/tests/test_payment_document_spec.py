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

from contexts.fraud_evaluation.domain import fraud_rules, sanctions
from contexts.payment_order_initiation.domain import enrichment_plan, payment_document
from contexts.payment_orchestration.domain import routing
from process.payment_context import PaymentContext

_SPEC_PATH = (
    pathlib.Path(__file__).resolve().parents[4] / "doinas-research" / "propose_payments.json"
)


def _schema():
    """The canonical `payments` schema. A plain function as well as a fixture, so the
    non-parametrised tests below can call it directly."""
    spec = json.loads(_SPEC_PATH.read_text())
    return spec["collections"]["payments"]["validator"]["$and"][0]["$jsonSchema"]


@pytest.fixture(scope="module")
def schema():
    return _schema()


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


# Fields written that the canonical spec does not declare. All legal — the spec sets no
# `additionalProperties: false` — and all deliberate:
#   isInternal / createdBy   pre-existing, unrelated to any stage
#   checks / authentication / entitlement
#       stage 2 (doc 15 B3/B1). `payments` has no field for any check, authentication or
#       approval outcome: `approvals[]` was prototyped and reverted
#       (`propose_payments.json:26`) and never restored when D2/D3 restored `lifecycle{}`
#       and `refs{}`. Added the same way those were — nullable, not `required` — and sent
#       to Doina as Q12 to ratify. This set is the tripwire: a SIXTH extra must be argued
#       for, not appear.
_KNOWN_EXTRAS = {
    "isInternal", "createdBy", "checks", "authentication", "entitlement",
    # Stage 3 (doc 17 B1/B5). The sixth, and the argument for it is in B5: her
    # L459-460 before/after screen cannot be drawn from a document that holds one
    # value per field. A SEVENTH must be argued for in the same way.
    "enrichment",
    # Stage 1 (DR-1.1): customer's own internal tracking reference (PO number,
    # contract ID), distinct from endToEndId. Doina's Aug 27 research doc asks for
    # it explicitly; the canonical `payments` spec does not declare it.
    "clientReference",
    # Stage 3 (doc L404): corridor category audit snapshot — "computed outcome
    # snapshot, not new instruction data." The EIGHTH; argued for the same way.
    "validation",
    # 2026-09-09 (Kiran): the step-up hold. Stage 2 leaves an over-threshold payment at
    # INITIATED with these set, so the channel can resume the SAME document after a second
    # factor. The NINTH and TENTH; argued for in the resume work, not slipped in.
    "stepUpRequired", "stepUpReason",
    # 2026-09-10: the bank's execution commitment, folded into `payments` per Doina's
    # Aug 27 target model (L427-429 strikes the `paymentOrders` collection through and
    # asks to add those fields directly in `payments`). The ELEVENTH; argued for in
    # `payment_document.build`'s `order` comment, not slipped in.
    "order",
    # 2026-09-11: the originator confirmation (BIAN PaymentConfirmation, SD 47766),
    # written at APPROVED (FR-4.4). The TWELFTH; argued for in `documents.confirmation`'s
    # docstring — a 1:1 artifact distinct from stage 5's `notifications`, folded onto
    # `payments` the same way `order` is.
    "confirmation",
}

# Enum values the code writes that are NOT in the canonical spec's enum for that field —
# admitted explicitly so the guard stays honest about what it allows, rather than silently
# passing an out-of-enum value (the 2026-04-24 `bian-mapping` anti-pattern).
#
# `PENDING_REVIEW` (Kiran, 2026-09-11, Q33 resolved): a REVIEW fraud decision holds the
# payment at this status (FR-4.13). The canonical `status` / `lifecycle.currentState` /
# `lifecycle.events[].state` enum declares `PENDING` (the sanctions/initiation status, a
# different thing) but not `PENDING_REVIEW`. Pending Doina's ratification into the canonical
# spec (`doinas-research/propose_payments.json` + the consolidated v35 model) — that edit
# lives outside this repo and is tracked as an out-of-repo follow-up. When it lands, remove
# this map and the guard reverts to the spec alone.
_ENUM_EXTENSIONS = {
    "status": {"PENDING_REVIEW"},
    "lifecycle.currentState": {"PENDING_REVIEW"},
    "lifecycle.events[].state": {"PENDING_REVIEW"},
}


def test_no_field_is_written_that_the_spec_does_not_declare(schema):
    doc = payment_document.build(_ctx())
    extras = set(doc) - set(schema["properties"])
    assert extras == _KNOWN_EXTRAS


def test_stage_two_slots_are_empty_at_creation():
    """Stage 2 fills these; stage 1 only opens the slot, exactly as it does for `fraud`.

    `checks` is `[]` rather than absent so a reader needs no `$exists` branch — but
    `append_checks` uses `$push`/`$each`, which creates the array either way, so documents
    written before this change stay readable."""
    doc = payment_document.build(_ctx())
    assert doc["checks"] == []
    assert doc["authentication"] is None
    assert doc["entitlement"] is None
    assert doc["enrichment"] is None
    assert doc["validation"] == {}
    assert doc["fxRate"] is None
    assert doc["clientReference"] is None


def _enum_fields(schema, prefix=""):
    """Every (dotted path, allowed values) pair the spec declares, at ANY depth.

    ⚠️ This used to walk `properties` and exactly ONE level of nested `properties`, and it
    never descended into `items.properties` — so **no array-element enum was checked at
    all**. Stage 3 shipped `fees[].type = "WIRE_TRANSFER_FEE"` straight past it, and
    `lifecycle.events[].state` / `.actorType` were unguarded too (defect 2026-09-01
    `guard-gap`). The recursion is the fix, and it is the whole point of the test: this
    file exists to discharge the 2026-04-28 enum-drift prevention rule, and a guard with a
    hole in it discharges nothing.

    An array is marked with a `[]` segment so a path reads `fees[].type` — `_dig` then knows
    to fan out over the elements rather than treating the array as a dict.
    """
    for name, prop in (schema.get("properties") or {}).items():
        path = f"{prefix}{name}"
        if "enum" in prop:
            yield path, prop["enum"]
        yield from _enum_fields(prop, prefix=f"{path}.")
        items = prop.get("items")
        if isinstance(items, dict):
            yield from _enum_fields(items, prefix=f"{path}[].")


def _dig(doc, dotted):
    """Resolve a dotted path, fanning out over `[]` segments.

    Returns a single value for a scalar path, or a list of the element values for a path
    that crosses an array. `_check_enum_values` treats both uniformly.
    """
    cur = doc
    for part in dotted.split("."):
        if part.endswith("[]"):
            cur = (cur or {}).get(part[:-2]) if isinstance(cur, dict) else None
            if not isinstance(cur, list):
                return []
            return [_dig(item, ".".join(_rest(dotted, part))) for item in cur]
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _rest(dotted, after):
    parts = dotted.split(".")
    return parts[parts.index(after) + 1:]


def _assert_enum_values_legal(schema, doc, where):
    """Shared by the built-document and post-stage-4 variants below."""
    for path, allowed in _enum_fields(schema):
        allowed_plus = set(allowed) | _ENUM_EXTENSIONS.get(path, set())
        value = _dig(doc, path)
        values = value if isinstance(value, list) else [value]
        for one in values:
            if one is None and None not in allowed:
                continue  # absent/unwritten is a required-field concern, tested above
            assert one in allowed_plus, (
                f"{where}: {path}={one!r} is not in the spec enum {allowed}"
            )


@pytest.mark.parametrize("rail", ["INTERNAL", "WIRE", "ACH", "CARD", "RTP"])
def test_every_written_enum_value_is_legal(schema, rail):
    doc = payment_document.build(_ctx(payment_rail=rail))
    _assert_enum_values_legal(schema, doc, f"as built ({rail})")


def test_the_enum_walk_reaches_inside_arrays():
    """The guard's own guard.

    `fees[].type` was illegal for the whole of stage 3 and this file did not notice, because
    the walk stopped at `properties` and never entered `items.properties`. Assert the walk
    now REACHES those paths — without this, a future refactor could quietly restore the
    one-level version and every enum test would still pass.
    """
    schema = _schema()
    paths = {path for path, _ in _enum_fields(schema)}
    assert "fees[].type" in paths
    assert "fees[].chargedTo" in paths
    assert "lifecycle.events[].state" in paths
    assert "lifecycle.events[].actorType" in paths


def test_enum_values_are_legal_after_every_stage_that_writes_them():
    """The document is written by five stages; the built document is only the first.

    `fees` is `[]` at build, so even a fully recursive walk over `payment_document.build`
    cannot see `fees[].type` — the value arrives at stage 3 enrichment. That is the second
    half of defect 2026-09-01: a spec-walking test must run over the document at every stage
    that writes it, or fields populated later are structurally invisible to it.
    """
    schema = _schema()
    doc = payment_document.build(_ctx(payment_rail="WIRE"))

    # Stage 3's enrichment output, applied the way `enrichment.py` applies it.
    doc["fees"] = [{
        "type": enrichment_plan.WIRE_FEE_TYPE,
        "amount": enrichment_plan.WIRE_FEE,
        "currency": "USD",
        "chargedTo": "DEBTOR",
    }]
    # Stage 4b's output.
    doc["fraud"] = {
        "alertId": "FRAUD-0000abcd",
        "score": 41,
        "decision": fraud_rules.APPROVED,
        "rulesFired": ["AMOUNT_TIER"],
        "checkedAt": doc["createdAt"],
    }
    doc["correspondent"]["sanctionsCheck"]["status"] = sanctions.CLEAR
    doc["wireDetails"]["network"] = routing.SWIFT

    _assert_enum_values_legal(schema, doc, "after stages 3 and 4")


def test_pending_review_is_admitted_as_a_documented_enum_extension():
    """FR-4.13 / Q33 (resolved 2026-09-11). PENDING_REVIEW is a Kiran-added lifecycle status
    the canonical enum does not declare. The guard admits it via _ENUM_EXTENSIONS so the
    addition is auditable rather than smuggled past (the 2026-04-24 anti-pattern). This pins
    both halves: the value is NOT in the spec enum, and the guard still accepts a document
    carrying it on all three enum-checked paths (status, currentState, events[].state).

    Self-destructs when the canonical spec is updated: if PENDING_REVIEW lands in the spec
    enum, the first assert fails and tells the next reader to drop it from _ENUM_EXTENSIONS
    and let the guard revert to the spec alone."""
    schema = _schema()
    assert "PENDING_REVIEW" not in schema["properties"]["status"]["enum"], (
        "PENDING_REVIEW is now in the canonical spec enum — remove it from "
        "_ENUM_EXTENSIONS and delete this test; the guard should revert to the spec alone."
    )
    doc = payment_document.build(_ctx(payment_rail="WIRE"))
    doc["lifecycle"]["currentState"] = "PENDING_REVIEW"
    doc["status"] = "PENDING_REVIEW"
    doc["lifecycle"]["events"].append({
        "state": "PENDING_REVIEW", "at": doc["createdAt"],
        "actor": "fraud-service", "actorType": "SERVICE", "reason": "held for review",
    })
    # No AssertionError => PENDING_REVIEW is admitted on all three paths.
    _assert_enum_values_legal(schema, doc, "at PENDING_REVIEW hold")


def test_the_fee_type_the_code_writes_is_in_the_spec_enum():
    """Named directly, because this is the value that actually drifted."""
    schema = _schema()
    allowed = schema["properties"]["fees"]["items"]["properties"]["type"]["enum"]
    assert enrichment_plan.WIRE_FEE_TYPE in allowed


def test_every_fraud_decision_and_network_the_code_can_emit_is_legal():
    """Enum parity for the two enums stage 4 introduces, asserted against the spec file
    rather than transcribed into a comment."""
    schema = _schema()
    assert set(fraud_rules.DECISIONS) <= set(
        schema["properties"]["fraud"]["properties"]["decision"]["enum"]
    )
    assert routing.networks_emitted() <= set(
        schema["properties"]["wireDetails"]["properties"]["network"]["enum"]
    )


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
