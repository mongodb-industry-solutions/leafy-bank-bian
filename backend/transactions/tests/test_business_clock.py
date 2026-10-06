"""Business clock (cutoff plan A1): real UTC for untagged payments, run offset for tagged."""

from datetime import datetime, timedelta, timezone

from contexts.payment_orchestration.domain import routing
from shared import business_clock
from tests.test_payments_service import (  # reuse the fixtures, don't fork them
    FakeCollection,
    _initiate,
    _initiate_external,
    db,
    service,
)

_ = (db, service)  # pytest fixtures, imported for use by name


def _within(a: datetime, b: datetime, seconds: float = 60) -> bool:
    return abs((a - b).total_seconds()) < seconds


# --- the clock ----------------------------------------------------------------

def test_now_without_a_run_id_is_real_utc():
    got = business_clock.now()
    assert got.tzinfo is not None and got.utcoffset() == timedelta(0)
    assert _within(got, datetime.now(timezone.utc), 5)


def test_a_tagged_run_adds_its_offset():
    clocks = FakeCollection(key="_id")
    run_id = business_clock.create_run(clocks, offset_seconds=3 * 3600)
    got = business_clock.now(run_id, clocks=clocks)
    assert _within(got, datetime.now(timezone.utc) + timedelta(hours=3), 5)


def test_offset_is_cached_for_five_seconds(monkeypatch):
    clocks = FakeCollection(key="_id")
    run_id = business_clock.create_run(clocks, offset_seconds=3600)
    real_monotonic = business_clock.time.monotonic
    business_clock.now(run_id, clocks=clocks)

    clocks.docs[0]["offsetSeconds"] = 7200
    cached = business_clock.now(run_id, clocks=clocks)
    assert _within(cached, datetime.now(timezone.utc) + timedelta(hours=1), 5)

    monkeypatch.setattr(business_clock.time, "monotonic", lambda: real_monotonic() + 6)
    refreshed = business_clock.now(run_id, clocks=clocks)
    assert _within(refreshed, datetime.now(timezone.utc) + timedelta(hours=2), 5)


def test_a_missing_run_falls_back_to_real_time():
    clocks = FakeCollection(key="_id")
    assert _within(business_clock.now("CLK-missing", clocks=clocks),
                   datetime.now(timezone.utc), 5)
    assert _within(business_clock.now("CLK-missing", clocks=None),
                   datetime.now(timezone.utc), 5)


def test_to_et_handles_dst():
    utc = timezone.utc
    # Summer (EDT, -4) and winter (EST, -5).
    assert business_clock.to_et(datetime(2026, 7, 1, 21, 30, tzinfo=utc)).hour == 17
    assert business_clock.to_et(datetime(2026, 1, 15, 22, 30, tzinfo=utc)).hour == 17
    # 2026-03-08: 02:00 EST jumps to 03:00 EDT.
    assert business_clock.to_et(datetime(2026, 3, 8, 6, 59, tzinfo=utc)).strftime("%H:%M") == "01:59"
    assert business_clock.to_et(datetime(2026, 3, 8, 7, 0, tzinfo=utc)).strftime("%H:%M") == "03:00"
    # 2026-11-01: 02:00 EDT falls back to 01:00 EST.
    assert business_clock.to_et(datetime(2026, 11, 1, 5, 59, tzinfo=utc)).strftime("%H:%M") == "01:59"
    assert business_clock.to_et(datetime(2026, 11, 1, 6, 0, tzinfo=utc)).strftime("%H:%M") == "01:00"
    assert business_clock.minutes_since_midnight_et(
        datetime(2026, 1, 15, 22, 31, tzinfo=utc)) == 17 * 60 + 31


def test_anchor_offset_lands_on_the_target_minute():
    target = 17 * 60 + 31
    for real_now in (
        datetime(2026, 7, 1, 13, 7, 42, tzinfo=timezone.utc),    # EDT morning
        datetime(2026, 1, 15, 23, 59, 59, tzinfo=timezone.utc),  # EST evening
        datetime(2026, 10, 6, 3, 0, 0, tzinfo=timezone.utc),     # ET is still the prior day
    ):
        offset = business_clock.anchor_offset(target, real_now=real_now)
        shifted = real_now + timedelta(seconds=offset)
        assert business_clock.minutes_since_midnight_et(shifted) == target
        assert business_clock.to_et(shifted).date() == business_clock.to_et(real_now).date()


# --- through the real saga ----------------------------------------------------

def _run_at(db, et_minutes: int) -> str:
    offset = business_clock.anchor_offset(et_minutes, real_now=datetime.now(timezone.utc))
    return business_clock.create_run(db["demoClocks"], offset_seconds=offset)


def test_tagged_payment_sees_offset_untagged_sees_real_time(service, db):
    run_id = _run_at(db, 17 * 60 + 31)
    tagged = _initiate(service, clock_run_id=run_id)
    untagged = _initiate(service)

    assert tagged["demo"] == {"clockRunId": run_id}
    assert business_clock.minutes_since_midnight_et(tagged["createdAt"]) == 17 * 60 + 31
    for event in tagged["lifecycle"]["events"]:
        assert business_clock.minutes_since_midnight_et(event["at"]) == 17 * 60 + 31

    real = datetime.now(timezone.utc)
    assert _within(untagged["createdAt"], real)
    assert all(_within(e["at"], real) for e in untagged["lifecycle"]["events"])


def test_an_untagged_document_is_unchanged(service, db):
    payment = _initiate(service)
    assert "demo" not in payment
    assert "demo" not in db["payments"].docs[-1]


def test_context_from_doc_restores_the_clock_run(service, db):
    run_id = _run_at(db, 17 * 60 + 31)
    payment = _initiate(service, clock_run_id=run_id)
    stored = db["payments"].find_one({"paymentId": payment["paymentId"]})

    ctx = service._context_from_doc(stored, customer_ref=stored["customerId"],
                                    authentication=None)
    assert ctx.clock_run_id == run_id
    assert business_clock.minutes_since_midnight_et(ctx.now) == 17 * 60 + 31


def test_api_rejects_demo_fields():
    """Only in-process callers may tag. `extra="forbid"` is what FastAPI turns into a 422."""
    import pytest
    from pydantic import ValidationError

    from api_models import PaymentOrderInitiateRequest

    with pytest.raises(ValidationError, match="demo"):
        PaymentOrderInitiateRequest(
            customerId="CUST-1", type="CREDIT_TRANSFER", rail="WIRE",
            debtor={"accountId": "ACC-debtor"},
            creditor={"accountNo": "123", "name": "Acme", "bic": "CHASUS33"},
            instructedAmount=100.0, instructedCurrency="USD",
            demo={"clockRunId": "CLK-x"},
        )


def test_routing_snapshot_cutoff_hour_unchanged_for_untagged(service, db):
    payment = _initiate_external(service)
    snapshot = db["routingSnapshots"].find_one(
        {"routingSnapshotId": payment["refs"]["routingSnapshotId"]}
    )
    # routing.py's hour table is untouched: the snapshot still reads its own hour, and only
    # the ET clock under it is now DST-correct.
    cutoff_hour = routing._CUTOFF_HOUR_ET[snapshot["clearingNetwork"]]
    assert snapshot["cutoffHourET"] == cutoff_hour
    real_et = business_clock.to_et(datetime.now(timezone.utc))
    assert snapshot["withinCutoff"] is (real_et.hour < cutoff_hour)
    assert "demo" not in payment
