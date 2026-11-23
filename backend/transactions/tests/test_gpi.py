"""Simulated SWIFT gpi — the pure script, event shape, filter and summary (GPI plan §3/§4)."""

from datetime import datetime, timedelta, timezone

import pytest

from contexts.payment_rail.domain import gpi

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _hop(seq, role, bic, member):
    return {"seq": seq, "role": role, "bic": bic, "country": "XX", "gpiMember": member}


def _route(n):
    hops = {
        2: [_hop(1, "DEBTOR_AGENT", "LEAFUS33", True),
            _hop(2, "CREDITOR_AGENT", "ROYCCAT2", False)],
        3: [_hop(1, "DEBTOR_AGENT", "LEAFUS33", True),
            _hop(2, "SENDER_CORRESPONDENT", "CHASUS33", True),
            _hop(3, "CREDITOR_AGENT", "BARCGB22", True)],
        4: [_hop(1, "DEBTOR_AGENT", "LEAFUS33", True),
            _hop(2, "SENDER_CORRESPONDENT", "CHASUS33", True),
            _hop(3, "INTERMEDIARY", "BARCGB22", True),
            _hop(4, "CREDITOR_AGENT", "NWBKGB2L", False)],
    }
    return hops[n]


PAYMENT = {"paymentId": "PAY-1", "rail": "WIRE", "uetr": "UETR-0f9a"}


@pytest.mark.parametrize("n", [2, 3, 4])
def test_a_credited_script_is_one_report_per_hop_ending_accc(n):
    steps = gpi.script(_route(n), gpi.CREDITED)
    assert [s.seq for s in steps] == list(range(1, n + 1))
    assert [s.gpi_status for s in steps] == [gpi.ACSP] * (n - 1) + [gpi.ACCC]
    assert steps[-1].outcome == gpi.CREDITED
    assert all(s.outcome is None for s in steps[:-1])


def test_in_flight_stops_before_the_creditor_agent():
    steps = gpi.script(_route(4))
    assert [s.seq for s in steps] == [1, 2, 3]
    assert {s.gpi_status for s in steps} == {gpi.ACSP}


def test_forwarding_reason_depends_on_the_next_agent_membership():
    steps = gpi.script(_route(4), gpi.CREDITED)
    assert [(s.from_bic, s.to_bic, s.reason_code) for s in steps[:3]] == [
        ("LEAFUS33", "CHASUS33", gpi.G000),
        ("CHASUS33", "BARCGB22", gpi.G000),
        ("BARCGB22", "NWBKGB2L", gpi.G001),  # NatWest is not a gpi member
    ]
    assert (steps[3].from_bic, steps[3].to_bic) == ("BARCGB22", "NWBKGB2L")


def test_a_return_follows_the_credit_at_its_own_seq():
    """RETURNED is after credit: the ACCC stays, the RJCT is the creditor agent's 2nd report."""
    steps = gpi.script(_route(4), gpi.RETURNED, reason_code="AC04")
    assert [(s.seq, s.gpi_status) for s in steps] == [
        (1, gpi.ACSP), (2, gpi.ACSP), (3, gpi.ACSP), (4, gpi.ACCC), (5, gpi.RJCT)]
    ret = steps[-1]
    assert (ret.reason_code, ret.outcome) == ("AC04", gpi.RETURNED)
    assert ret.hop["seq"] == 4 and ret.hop["role"] == "CREDITOR_AGENT"
    assert (ret.from_bic, ret.to_bic) == ("NWBKGB2L", "BARCGB22"), "the leg back"
    ids = {gpi.event_document(PAYMENT, s, at=T0, direction=gpi.OUTBOUND)["_id"] for s in steps}
    assert len(ids) == 5, "the return does not collide with the ACCC"


def test_a_rejection_at_the_creditor_agent_is_pre_settlement():
    steps = gpi.script(_route(4), gpi.REJECTED, reason_code="AC04")
    assert [s.gpi_status for s in steps] == [gpi.ACSP, gpi.ACSP, gpi.ACSP, gpi.RJCT]
    assert (steps[-1].seq, steps[-1].outcome) == (4, gpi.REJECTED)


def test_accc_carries_no_reason_code():
    assert gpi.script(_route(3), gpi.CREDITED)[-1].reason_code is None


def test_a_rejection_at_the_first_agent_stops_there():
    steps = gpi.script(_route(4), gpi.REJECTED, reject_at=2, reason_code="FF01")
    assert [(s.seq, s.gpi_status) for s in steps] == [(1, gpi.ACSP), (2, gpi.RJCT)]
    assert steps[-1].hop["bic"] == "CHASUS33"
    assert steps[-1].outcome == gpi.REJECTED


def test_no_route_no_script():
    assert gpi.script([], gpi.CREDITED) == []
    assert gpi.script(None) == []


def test_reportable_is_a_member_hop_or_a_final_status():
    steps = gpi.script(_route(4), gpi.CREDITED)
    assert [gpi.is_reportable(s) for s in steps] == [True, True, True, True]
    hop = _hop(1, "INTERMEDIARY", "XXXXGB22", False)
    assert not gpi.is_reportable(gpi.Step(1, hop, "A", "B", gpi.ACSP, gpi.G001))
    assert gpi.is_reportable(gpi.Step(1, hop, "A", "B", gpi.RJCT, "AC04", gpi.RETURNED))


def test_event_document_shape_and_deterministic_id():
    step = gpi.script(_route(4), gpi.CREDITED)[2]
    doc = gpi.event_document(PAYMENT, step, at=T0, direction=gpi.OUTBOUND)
    assert doc["_id"] == "PAY-1:GPI:3"
    assert doc == gpi.event_document(PAYMENT, step, at=T0, direction=gpi.OUTBOUND)
    assert set(doc) == {"_id", "paymentId", "rail", "kind", "seq", "at", "uetr", "direction",
                        "hop", "fromBic", "toBic", "gpiStatus", "reasonCode", "outcome",
                        "reportable", "simulated"}
    assert doc["kind"] == "GPI" and doc["simulated"] is True
    assert doc["uetr"] == "0f9a", "stored bare, whatever prefix the payment carries"
    assert doc["hop"] == {"seq": 3, "role": "INTERMEDIARY", "bic": "BARCGB22", "country": "XX"}


def test_stamps_are_spaced_from_the_first_report():
    assert [gpi.stamp(T0, k) for k in range(3)] == [
        T0, T0 + timedelta(seconds=5), T0 + timedelta(seconds=10)]


def _docs(steps, at=T0):
    return [gpi.event_document(PAYMENT, s, at=at, direction=gpi.OUTBOUND) for s in steps]


@pytest.mark.parametrize("steps,status,confirmed", [
    (gpi.script(_route(4))[:1], gpi.SENT, 1),
    (gpi.script(_route(4)), gpi.IN_TRANSIT, 3),
    (gpi.script(_route(4), gpi.CREDITED), gpi.CREDITED, 4),
    (gpi.script(_route(4), gpi.RETURNED), gpi.RETURNED, 4),
    # The refusing hop confirmed nothing.
    (gpi.script(_route(4), gpi.REJECTED, reject_at=2), gpi.REJECTED, 1),
    (gpi.script(_route(4), gpi.REJECTED), gpi.REJECTED, 3),
])
def test_summary_reads_the_latest_report(steps, status, confirmed):
    tracking = gpi.summary(_docs(steps), 4)
    assert tracking["trackingStatus"] == status
    assert tracking["trackingStatus"] in gpi.TRACKING_STATUSES
    assert tracking["hopsTotal"] == 4
    assert tracking["hopsConfirmed"] == confirmed
    assert tracking["lastBic"] == steps[-1].hop["bic"]


def test_the_stored_uetr_is_bare_and_lowercase():
    step = gpi.script(_route(2))[0]
    doc = gpi.event_document({**PAYMENT, "uetr": "UETR-0F9A-ABC"}, step, at=T0,
                             direction=gpi.OUTBOUND)
    assert doc["uetr"] == "0f9a-abc"


def test_summary_is_order_independent_and_none_when_untracked():
    docs = _docs(gpi.script(_route(4), gpi.CREDITED))
    assert gpi.summary(list(reversed(docs)), 4) == gpi.summary(docs, 4)
    assert gpi.summary([], 4) is None


def test_inbound_route_ends_at_us():
    payment = {"debtor": {"bic": "DEUTDEFF", "bankCountry": "DE"}}
    route = gpi.inbound_route(payment, "LEAFUS33", "US")
    [step] = [s for s in gpi.script(route, gpi.CREDITED) if s.seq == len(route)]
    assert (step.from_bic, step.to_bic, step.gpi_status) == ("DEUTDEFF", "LEAFUS33", gpi.ACCC)
