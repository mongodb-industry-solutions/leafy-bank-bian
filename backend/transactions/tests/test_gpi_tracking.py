"""GPI tracking — emit, the `payments.tracking` summary, the validator branch, and the
stage hooks driven through the real saga (GPI plan §3, decisions 1-3)."""

from datetime import datetime, timedelta, timezone

import logging

import pytest
from pymongo.errors import BulkWriteError, WriteError

from contexts.payment_rail.application import gpi_tracking
from contexts.payment_rail.domain import gpi
from contexts.payment_settlement import settle
from tests.test_payments_service import (  # reuse the fixtures, don't fork them
    DEBTOR,
    FakeCollection,
    FakeConnection,
    FakeDb,
    _initiate,
    _initiate_external,
    db,       # noqa: F401 - pytest fixture
    service,  # noqa: F401 - pytest fixture
)

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

ROUTE = [
    {"seq": 1, "role": "DEBTOR_AGENT", "bic": "LEAFUS33", "country": "US", "gpiMember": True},
    {"seq": 2, "role": "SENDER_CORRESPONDENT", "bic": "CHASUS33", "country": "US",
     "gpiMember": True},
    {"seq": 3, "role": "CREDITOR_AGENT", "bic": "BARCGB22", "country": "GB", "gpiMember": True},
]
PAYMENT = {"paymentId": "PAY-1", "rail": "WIRE", "uetr": "u-1", "tracking": None}


def _db():
    return FakeDb({"payments": FakeCollection([dict(PAYMENT)], key="paymentId"),
                   "paymentEvents": FakeCollection([], key="_id")})


def _gpi(db):
    return sorted((d for d in db["paymentEvents"].docs if d["kind"] == "GPI"),
                  key=lambda d: d["seq"])


def _tracking(db):
    return db["payments"].docs[0].get("tracking")


# --- emit ---------------------------------------------------------------------

def test_emit_is_idempotent_and_keeps_the_first_timestamp():
    db = _db()
    steps = gpi.script(ROUTE, gpi.CREDITED)
    gpi_tracking.emit(db["payments"], PAYMENT, steps, now=T0, direction=gpi.OUTBOUND,
                      hops_total=3)
    first = [e["at"] for e in _gpi(db)]
    gpi_tracking.emit(db["payments"], PAYMENT, steps, now=T0 + timedelta(minutes=5),
                      direction=gpi.OUTBOUND, hops_total=3)
    events = _gpi(db)
    assert [e["_id"] for e in events] == ["PAY-1:GPI:1", "PAY-1:GPI:2", "PAY-1:GPI:3"]
    assert [e["at"] for e in events] == first
    assert first[0] == T0 and first == sorted(set(first)), "same instant, still ordered"


def test_emit_extends_the_story_and_the_summary_follows():
    db = _db()
    gpi_tracking.emit(db["payments"], PAYMENT, gpi.script(ROUTE)[:1], now=T0,
                      direction=gpi.OUTBOUND, hops_total=3)
    assert _tracking(db)["trackingStatus"] == gpi.SENT
    gpi_tracking.emit(db["payments"], PAYMENT, gpi.script(ROUTE), now=T0,
                      direction=gpi.OUTBOUND, hops_total=3, base=T0)
    assert _tracking(db)["trackingStatus"] == gpi.IN_TRANSIT
    gpi_tracking.emit(db["payments"], PAYMENT, gpi.script(ROUTE, gpi.CREDITED),
                      now=T0 + timedelta(seconds=30), direction=gpi.OUTBOUND, hops_total=3,
                      base=T0)
    tracking = _tracking(db)
    assert tracking["trackingStatus"] == gpi.CREDITED
    assert (tracking["hopsConfirmed"], tracking["hopsTotal"]) == (3, 3)
    assert tracking["lastBic"] == "BARCGB22"


def test_emit_writes_events_and_summary_together():
    db = _db()

    def boom(*a, **kw):
        raise RuntimeError("payments update failed")

    db["payments"].update_one = boom
    with pytest.raises(RuntimeError):
        gpi_tracking.emit(db["payments"], PAYMENT, gpi.script(ROUTE), now=T0,
                          direction=gpi.OUTBOUND, hops_total=3)
    assert _gpi(db) == [], "the event insert rolled back with the summary update"


def test_a_return_after_the_credit_is_recorded_not_dropped():
    """RJCT after ACCC: a second report from the creditor agent, at seq N+1."""
    db = _db()
    gpi_tracking.track(db["payments"], PAYMENT, now=T0, route=ROUTE, outcome=gpi.CREDITED)
    gpi_tracking.track(db["payments"], PAYMENT, now=T0 + timedelta(days=1), route=ROUTE,
                       outcome=gpi.RETURNED, reason_code="AC04")
    events = _gpi(db)
    assert [(e["seq"], e["gpiStatus"]) for e in events] == [
        (1, "ACSP"), (2, "ACSP"), (3, "ACCC"), (4, "RJCT")]
    assert events[-1]["_id"] == "PAY-1:GPI:4"
    assert (events[-1]["outcome"], events[-1]["hop"]["seq"]) == (gpi.RETURNED, 3)
    assert _tracking(db)["trackingStatus"] == gpi.RETURNED
    assert _tracking(db)["hopsConfirmed"] == 3
    # Idempotent: the same return again adds nothing.
    gpi_tracking.track(db["payments"], PAYMENT, now=T0 + timedelta(days=2), route=ROUTE,
                       outcome=gpi.RETURNED, reason_code="AC04")
    assert len(_gpi(db)) == 4


def test_any_refusal_after_a_credit_reads_returned():
    db = _db()
    gpi_tracking.track(db["payments"], PAYMENT, now=T0, route=ROUTE, outcome=gpi.CREDITED)
    gpi_tracking.track(db["payments"], PAYMENT, now=T0, route=ROUTE, outcome=gpi.REJECTED,
                       reason_code="AC04")
    last = _gpi(db)[-1]
    assert (last["seq"], last["gpiStatus"], last["outcome"]) == (4, "RJCT", gpi.RETURNED)
    assert last["at"] > _gpi(db)[-2]["at"]
    assert _tracking(db)["trackingStatus"] == gpi.RETURNED


def test_a_status_after_a_final_is_appended_not_collided():
    db = _db()
    gpi_tracking.track(db["payments"], PAYMENT, now=T0, route=ROUTE, outcome=gpi.REJECTED,
                       reject_at=2, reason_code="FF01")
    gpi_tracking.track(db["payments"], PAYMENT, now=T0 + timedelta(hours=1), route=ROUTE,
                       outcome=gpi.CREDITED)
    events = _gpi(db)
    assert [(e["seq"], e["gpiStatus"]) for e in events] == [
        (1, "ACSP"), (2, "RJCT"), (4, "ACCC")], "past every hop, forwards not re-emitted"
    assert _tracking(db)["trackingStatus"] == gpi.CREDITED


def test_hop_timestamps_are_strictly_increasing_when_the_clock_moves_backwards():
    """A business clock reset below the first report: the story still reads in order."""
    db = _db()
    gpi_tracking.track(db["payments"], PAYMENT, now=T0, route=ROUTE, first_only=True)
    gpi_tracking.track(db["payments"], PAYMENT, now=T0 + timedelta(milliseconds=2),
                       route=ROUTE)
    gpi_tracking.track(db["payments"], PAYMENT, now=T0 - timedelta(hours=1), route=ROUTE,
                       outcome=gpi.CREDITED)
    ats = [e["at"] for e in _gpi(db)]
    assert ats == sorted(set(ats))
    assert ats[1] - ats[0] == timedelta(seconds=gpi.HOP_SPACING_SECONDS), "visibly spaced"


def test_a_validation_failure_is_logged_once_with_the_fix(monkeypatch, caplog):
    monkeypatch.setattr(gpi_tracking, "_validation_failure_reported", False)
    failures = [BulkWriteError({"writeErrors": [{"code": 121, "errmsg": "failed"}]}),
                WriteError("Document failed validation", code=121)]

    def reject(*a, **kw):
        raise failures.pop(0)

    monkeypatch.setattr(gpi_tracking, "emit", reject)
    with caplog.at_level(logging.DEBUG, logger=gpi_tracking.__name__):
        gpi_tracking.track(_db()["payments"], PAYMENT, now=T0, route=ROUTE)
        gpi_tracking.track(_db()["payments"], PAYMENT, now=T0, route=ROUTE)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "python -m data.ensure_indexes" in errors[0].getMessage()


def test_any_other_failure_is_still_logged_every_time(monkeypatch, caplog):
    def boom(*a, **kw):
        raise BulkWriteError({"writeErrors": [{"code": 11000}]})

    monkeypatch.setattr(gpi_tracking, "emit", boom)
    with caplog.at_level(logging.ERROR, logger=gpi_tracking.__name__):
        gpi_tracking.track(_db()["payments"], PAYMENT, now=T0, route=ROUTE)
        gpi_tracking.track(_db()["payments"], PAYMENT, now=T0, route=ROUTE)
    assert [r.getMessage() for r in caplog.records].count(
        "gpi tracking failed for PAY-1") == 2


def test_track_skips_a_payment_without_a_uetr_or_a_route():
    db = _db()
    gpi_tracking.track(db["payments"], {**PAYMENT, "uetr": None}, now=T0, route=ROUTE)
    gpi_tracking.track(db["payments"], PAYMENT, now=T0, route=[])
    # A legacy snapshot carries no `route`.
    db["routingSnapshots"].insert_one({"routingSnapshotId": "RS-1"})
    gpi_tracking.track(db["payments"], {**PAYMENT, "refs": {"routingSnapshotId": "RS-1"}},
                       now=T0)
    assert _gpi(db) == [] and _tracking(db) is None


def test_track_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("validator not deployed yet")

    monkeypatch.setattr(gpi_tracking, "emit", boom)
    gpi_tracking.track(_db()["payments"], PAYMENT, now=T0, route=ROUTE)  # no raise


# --- the validator -------------------------------------------------------------
#
# `jsonschema` is not in the venv, so a checker for the $jsonSchema subset the REAL validator
# uses (bsonType incl. lists, required, enum, properties, minimum, oneOf). It is asserted to
# cover every keyword the validator contains, so a new keyword fails here instead of being
# silently ignored.

_BSON_TYPES = {
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool) and -2**31 <= v < 2**31,
    "date": lambda v: isinstance(v, datetime),
    "string": lambda v: isinstance(v, str),
    "bool": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}
_KEYWORDS = {"bsonType", "required", "enum", "properties", "minimum", "oneOf"}


def _schema():
    from data import ensure_indexes
    return ensure_indexes.PAYMENT_EVENTS_VALIDATOR["$jsonSchema"]


def _keywords(schema):
    found = set(schema)
    for sub in (schema.get("properties") or {}).values():
        found |= _keywords(sub)
    for sub in schema.get("oneOf") or []:
        found |= _keywords(sub)
    return found


def _errors(schema, value, path="$"):
    if "bsonType" in schema:
        types = schema["bsonType"]
        types = types if isinstance(types, list) else [types]
        if not any(_BSON_TYPES[t](value) for t in types):
            return [f"{path}: bsonType {types}"]
    out = []
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: enum")
    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        out.append(f"{path}: minimum")
    if isinstance(value, dict):
        out += [f"{path}.{k}: required" for k in schema.get("required", []) if k not in value]
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                out += _errors(sub, value[key], f"{path}.{key}")
        if "oneOf" in schema:
            matched = sum(1 for sub in schema["oneOf"] if not _errors(sub, value, path))
            if matched != 1:
                out.append(f"{path}: oneOf matched {matched}")
    return out


def test_the_checker_covers_every_keyword_of_the_real_validator():
    assert _keywords(_schema()) <= _KEYWORDS


def _outbound_docs():
    route4 = ROUTE[:2] + [
        {"seq": 3, "role": "INTERMEDIARY", "bic": "BARCGB22", "country": "GB",
         "gpiMember": True},
        {"seq": 4, "role": "CREDITOR_AGENT", "bic": "NWBKGB2L", "country": "GB",
         "gpiMember": False}]
    for route in (ROUTE, route4):
        for outcome in (None, gpi.CREDITED, gpi.RETURNED, gpi.REJECTED):
            for step in gpi.script(route, outcome):
                yield gpi.event_document(PAYMENT, step, at=T0, direction=gpi.OUTBOUND)
        yield from (gpi.event_document(PAYMENT, step, at=T0, direction=gpi.OUTBOUND)
                    for step in gpi.script(route, gpi.REJECTED, reject_at=2,
                                           reason_code=gpi.REASON_INVALID_FORMAT))


def _inbound_docs():
    payment = {"paymentId": "PAY-IN", "rail": "WIRE", "uetr": "ABC-INBOUND",
               "debtor": {"bic": "DEUTDEFF", "bankCountry": "DE"}}
    route = gpi.inbound_route(payment, "LEAFUS33", "US")
    for outcome in (gpi.CREDITED, gpi.REJECTED):
        for step in gpi.script(route, outcome, reason_code="AC03"):
            if step.seq == len(route):
                yield gpi.event_document(payment, step, at=T0, direction=gpi.INBOUND)
    # An inbound sender's hop has no BIC on file.
    bare = gpi.inbound_route({"paymentId": "PAY-IN"}, "LEAFUS33", "US")
    yield gpi.event_document({**payment, "debtor": {}}, gpi.script(bare, gpi.CREDITED)[-1],
                             at=T0, direction=gpi.INBOUND)


def test_the_real_validator_accepts_every_document_the_writer_produces():
    docs = list(_outbound_docs()) + list(_inbound_docs())
    assert {d["outcome"] for d in docs} == {None, *gpi.OUTCOMES}
    assert {d["direction"] for d in docs} == set(gpi.DIRECTIONS)
    for doc in docs:
        assert _errors(_schema(), doc) == [], doc["_id"]


def test_the_real_validator_accepts_documents_written_through_emit():
    db = _db()
    gpi_tracking.track(db["payments"], PAYMENT, now=T0, route=ROUTE, outcome=gpi.CREDITED)
    gpi_tracking.track(db["payments"], PAYMENT, now=T0, route=ROUTE, outcome=gpi.RETURNED)
    assert len(_gpi(db)) == 4
    for doc in _gpi(db):
        assert _errors(_schema(), doc) == [], doc["_id"]


def _valid():
    return gpi.event_document(PAYMENT, gpi.script(ROUTE)[0], at=T0, direction=gpi.OUTBOUND)


@pytest.mark.parametrize("field,value", [
    ("seq", "1"), ("seq", 1.0), ("seq", 0), ("seq", True), ("at", T0.isoformat()),
    ("uetr", None), ("reportable", "yes"), ("simulated", 1), ("hop", "LEAFUS33"),
    ("gpiStatus", "PDNG"), ("direction", "SIDEWAYS"), ("outcome", "LOST"), ("reasonCode", 0),
])
def test_the_real_validator_rejects_wrong_types_and_values(field, value):
    assert _errors(_schema(), {**_valid(), field: value}) != []


def test_the_real_validator_rejects_an_unknown_hop_role_and_missing_fields():
    doc = _valid()
    assert _errors(_schema(), {**doc, "hop": {**doc["hop"], "role": "X"}}) != []
    doc.pop("reportable")
    assert _errors(_schema(), doc) != []


def test_a_gpi_document_matches_exactly_one_oneof_branch():
    schema = _schema()
    matching = [b for b in schema["oneOf"] if not _errors(b, _valid())]
    assert len(matching) == 1
    # A GPI doc that also carried EVENT's fields still matches only the GPI branch (kind).
    doc = {**_valid(), "state": "X", "actor": "a", "actorType": "SYSTEM", "reason": "r"}
    assert _errors(schema, doc) == []


def test_gpi_ids_cannot_collide_with_event_or_check_ids():
    from contexts.payment_order_initiation.domain import lifecycle_history as lh
    gpi_id = gpi.event_document(PAYMENT, gpi.script(ROUTE)[0], at=T0,
                                direction=gpi.OUTBOUND)["_id"]
    assert gpi_id not in {lh.history_id("PAY-1", lh.EVENT, 1), lh.history_id("PAY-1", lh.CHECK, 1)}


# --- the hooks, through the saga ------------------------------------------------

def _initiate_to_gb(svc, **over):
    kwargs = dict(
        creditor_account_ref=None,
        creditor_party={"accountNo": "9876543210", "name": "Acme Corp", "bic": "NWBKGB2L",
                        "bankName": "NatWest", "bankCountry": "GB"},
        payment_rail="WIRE",
        wire_details={"wireType": "INTERNATIONAL",
                      "messageDefinitionIdentifier": "pain.001.001.09", "network": "SWIFT"},
    )
    return _initiate(svc, **{**kwargs, **over})


def test_an_international_wire_is_in_transit_after_capture_and_credited_at_completion(
        service, db):
    _initiate_to_gb(service)
    events = _gpi(db)
    assert [e["hop"]["bic"] for e in events] == ["LEAFUS33", "CHASUS33", "BARCGB22"]
    assert {e["gpiStatus"] for e in events} == {gpi.ACSP}
    assert _tracking(db)["trackingStatus"] == gpi.IN_TRANSIT

    settle.complete_due(FakeConnection(db), "db", delay_seconds=0)

    events = _gpi(db)
    assert [e["gpiStatus"] for e in events][-1] == gpi.ACCC
    assert events[-1]["hop"]["role"] == "CREDITOR_AGENT"
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    ats = [e["at"] for e in events]
    # Strictly increasing; the forwards are 5 s apart, so a completion run sooner than that
    # (delay 0 here) stamps the ACCC just after the last hop rather than before it.
    assert ats == sorted(set(ats))
    assert _tracking(db)["trackingStatus"] == gpi.CREDITED
    assert _tracking(db)["outcome"] == gpi.CREDITED


def test_completion_is_credited_when_stored_hops_read_back_naive(service, db):
    """Real Mongo (client not tz_aware) returns stored `at` naive while the completion clock
    is aware. Comparing them raised TypeError, which `track` swallowed, so live payments
    stayed IN_TRANSIT after settlement (PAY-ac2e2c3c, 2026-11-23)."""
    _initiate_to_gb(service)
    for doc in db["paymentEvents"].docs:
        if doc.get("kind") == gpi.KIND:
            doc["at"] = doc["at"].replace(tzinfo=None)

    settle.complete_due(FakeConnection(db), "db", delay_seconds=0)

    assert _gpi(db)[-1]["gpiStatus"] == gpi.ACCC
    assert _tracking(db)["trackingStatus"] == gpi.CREDITED


def test_completion_on_a_demo_clock_uses_business_time(service, db):
    _initiate_to_gb(service)
    db["cutoffWorkflow"].insert_one({"_id": "CLK-x", "kind": "CLOCK", "offsetSeconds": 3600})
    db["payments"].docs[0]["demo"] = {"clockRunId": "CLK-x"}

    settle.complete_due(FakeConnection(db), "db", delay_seconds=0)

    accc = _gpi(db)[-1]
    assert accc["at"] > datetime.now(timezone.utc) + timedelta(minutes=50)
    ats = [e["at"] for e in _gpi(db)]
    assert ats == sorted(set(ats)), "strictly increasing under the business clock"


def test_completion_on_a_clock_behind_the_hops_still_ends_last(service, db):
    _initiate_to_gb(service)
    db["cutoffWorkflow"].insert_one({"_id": "CLK-y", "kind": "CLOCK", "offsetSeconds": -3600})
    db["payments"].docs[0]["demo"] = {"clockRunId": "CLK-y"}

    settle.complete_due(FakeConnection(db), "db", delay_seconds=0)

    events = _gpi(db)
    ats = [e["at"] for e in events]
    assert events[-1]["gpiStatus"] == gpi.ACCC
    assert ats == sorted(set(ats))
    gaps = {b - a for a, b in zip(ats[:-2], ats[1:-1])}
    assert gaps == {timedelta(seconds=gpi.HOP_SPACING_SECONDS)}, "forwards visibly spaced"


def test_a_settlement_exception_is_a_pre_settlement_rejection(service, db):
    """Nothing was credited, so the tracker reads REJECTED; the lifecycle stays RETURNED."""
    _initiate_to_gb(service, settlement_outcome="EXCEPTION")
    payment = db["payments"].docs[0]
    last = _gpi(db)[-1]
    assert (last["gpiStatus"], last["outcome"], last["hop"]["role"]) == (
        gpi.RJCT, gpi.REJECTED, "CREDITOR_AGENT")
    assert last["reasonCode"] == payment["clearing"]["returnCode"], "the rail's own code"
    assert last["reportable"] is True
    assert gpi.ACCC not in {e["gpiStatus"] for e in _gpi(db)}
    assert _tracking(db)["trackingStatus"] == gpi.REJECTED
    assert _tracking(db)["hopsConfirmed"] == 3, "the refusing creditor agent is not counted"
    assert payment["lifecycle"]["currentState"] == "RETURNED"


@pytest.mark.parametrize("creditor_bic,route", [
    # BARCGB22 is our GB correspondent: paying its customer is the 2-hop route.
    ("BARCGB22", [("DEBTOR_AGENT", "LEAFUS33"), ("CREDITOR_AGENT", "BARCGB22")]),
    ("NWBKGB2L", [("DEBTOR_AGENT", "LEAFUS33"), ("INTERMEDIARY", "BARCGB22"),
                  ("CREDITOR_AGENT", "NWBKGB2L")]),
])
def test_a_non_usd_wire_has_no_sender_correspondent_end_to_end(service, db, creditor_bic,
                                                                 route):
    """Decision 5: GBP leaves LEAFUS33 with no sender correspondent — the creditor's bank
    is InstdAgt, and the message settles by clearing, not INDA."""
    from contexts.payment_rail.domain import pacs008

    for account in db["accounts"].docs:
        if account["accountId"] == DEBTOR:
            account["currency"] = "GBP"  # the payment's currency is the debtor account's
    _initiate_to_gb(service, instructed_currency="GBP", creditor_party={
        "accountNo": "9876543210", "name": "Acme Corp", "bic": creditor_bic,
        "bankName": "UK Bank", "bankCountry": "GB"})
    assert db["payments"].docs[0]["currency"] == "GBP"
    snapshot = db["routingSnapshots"].docs[0]
    assert [(h["role"], h["bic"]) for h in snapshot["route"]] == route
    body = pacs008.body(pacs008.build(db["payments"].docs[0], snapshot=snapshot))
    assert body["GrpHdr"]["InstdAgt"]["FinInstnId"]["BICFI"] == creditor_bic
    assert body["GrpHdr"]["SttlmInf"]["SttlmMtd"] != pacs008.SETTLEMENT_COVER

    settle.complete_due(FakeConnection(db), "db", delay_seconds=0)
    events = _gpi(db)
    assert [e["hop"]["role"] for e in events][0] == "DEBTOR_AGENT"
    assert [e["gpiStatus"] for e in events][-1] == gpi.ACCC
    assert len(events) == len(snapshot["route"])
    ats = [e["at"] for e in events]
    assert ats == sorted(set(ats))
    assert _tracking(db)["trackingStatus"] == gpi.CREDITED


def test_a_domestic_wire_is_not_tracked(service, db):
    _initiate_external(service, wire_details={
        "wireType": "DOMESTIC", "messageDefinitionIdentifier": "pain.001.001.09",
        "network": "FEDWIRE"})
    settle.complete_due(FakeConnection(db), "db", delay_seconds=0)
    assert _gpi(db) == []
    assert _tracking(db) is None


def test_an_internal_transfer_is_not_tracked(service, db):
    _initiate(service)
    assert _gpi(db) == [] and _tracking(db) is None


def test_a_usd_wire_to_a_chase_customer_names_chase_once_end_to_end(service, db):
    """Minor 2: creditor == our USD correspondent — no duplicate hop, and the pacs.008 is
    instructed to CHASUS33 as the creditor's bank (InstdAgt == CdtrAgt)."""
    from contexts.payment_rail.domain import pacs008

    _initiate_to_gb(service, creditor_party={
        "accountNo": "9876543210", "name": "Acme Corp", "bic": "CHASUS33",
        "bankName": "JPMorgan Chase Bank, N.A.", "bankCountry": "US"})
    snapshot = db["routingSnapshots"].docs[0]
    assert [(h["role"], h["bic"]) for h in snapshot["route"]] == [
        ("DEBTOR_AGENT", "LEAFUS33"), ("CREDITOR_AGENT", "CHASUS33")]
    body = pacs008.body(pacs008.build(db["payments"].docs[0], snapshot=snapshot))
    assert body["GrpHdr"]["InstdAgt"]["FinInstnId"]["BICFI"] == "CHASUS33"
    assert body["CdtTrfTxInf"][0]["CdtrAgt"]["FinInstnId"]["BICFI"] == "CHASUS33"
    assert [e["hop"]["bic"] for e in _gpi(db)] == ["LEAFUS33"]
