"""W1 write-skew showcase: route wiring and the shared guard filter (hermetic)."""

from types import SimpleNamespace

from contexts.payment_order_initiation.application import concurrency_demo
from contexts.payment_order_initiation.application import funds_reservation as fr
from routers import workflow
from tests.test_payments_service import DEBTOR, FakeCollection, db  # noqa: F401  (fixtures)


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        connection=SimpleNamespace(uri="mongodb://unused"), db_name="testdb",
        payments_service=SimpleNamespace(payment_limit_usd=1.0))))


def test_route_returns_the_run_report(monkeypatch):
    seen = {}

    def fake_run(base_service, connection, db_name):
        seen["db_name"] = db_name
        return {"runId": "abc", "attempts": [], "summary": {"invariantHeld": True}}

    monkeypatch.setattr(concurrency_demo, "run", fake_run)
    response = workflow.run_concurrency_payments(_request())

    assert response.status_code == 200
    assert seen["db_name"] == "testdb"
    assert b'"invariantHeld":true' in response.body


def test_reserve_uses_the_guarded_filter_the_demo_shows(db, monkeypatch):
    calls = []
    original = FakeCollection.find_one_and_update

    def spy(self, flt, *args, **kwargs):
        calls.append(flt)
        return original(self, flt, *args, **kwargs)

    monkeypatch.setattr(FakeCollection, "find_one_and_update", spy)
    db["payments"].docs.append({"paymentId": "PAY-w1", "instructedAmount": 80.0,
                                "debtor": {"accountId": DEBTOR}, "refs": {}})
    ctx = SimpleNamespace(
        collections=SimpleNamespace(db=db, accounts=db["accounts"], payments=db["payments"]),
        payment_id="PAY-w1", instructed_amount=80.0, debtor_account_ref=DEBTOR,
    )
    fr.reserve(ctx)

    assert fr.guarded_filter(DEBTOR, 80.0) in calls
