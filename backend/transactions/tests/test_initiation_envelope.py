"""Unit tests for the stage-1 rail initiation envelopes (`initiation_envelope.py`).

Pure — no database, no fakes. The gate this file exists for: for every rail in the
spec's enum, exactly ONE envelope is populated and the other two are all-null. That
invariant is what lets consumers discriminate on `rail` alone (R4).

The five rails are the spec's `rail` enum verbatim. CARD and RTP have no envelope of
their own, so they must produce three null ones — a regression there would silently
put wire fields on a card payment.
"""

import json
import pathlib

import pytest
from bson import ObjectId

from contexts.payment_order_initiation.domain import initiation_envelope as env

OID = ObjectId("661a4e0583b3a4567890abcd")
RAILS = ["INTERNAL", "WIRE", "ACH", "CARD", "RTP"]
ENVELOPE_FOR_RAIL = {"WIRE": "wireDetails", "ACH": "achDetails", "INTERNAL": "internalDetails"}

_SPEC = pathlib.Path(__file__).resolve().parents[4] / "doinas-research" / "propose_payments.json"


def _all_null(block):
    return all(v is None for v in block.values())


# --- the gate -----------------------------------------------------------------

@pytest.mark.parametrize("rail", RAILS)
def test_exactly_one_envelope_populated(rail):
    out = env.build_envelopes(
        rail=rail,
        payment_oid=OID,
        debtor_bank_country="US",
        creditor_bank_country="US",
    )
    assert set(out) == {"wireDetails", "achDetails", "internalDetails"}

    populated = ENVELOPE_FOR_RAIL.get(rail)
    for name, block in out.items():
        if name == populated:
            assert not _all_null(block), f"{name} should be populated for rail={rail}"
        else:
            assert _all_null(block), f"{name} must be all-null for rail={rail}"


@pytest.mark.parametrize("rail", ["CARD", "RTP"])
def test_rails_without_an_envelope_get_three_null_ones(rail):
    out = env.build_envelopes(rail=rail, payment_oid=OID)
    assert all(_all_null(b) for b in out.values())


@pytest.mark.parametrize("rail", RAILS)
def test_envelope_keys_match_the_spec(rail):
    """Every key the builder emits must exist in the spec, and none may be missing —
    the enum-drift prevention rule applied to object shape (defect 2026-04-28)."""
    spec = json.loads(_SPEC.read_text())
    props = spec["collections"]["payments"]["validator"]["$and"][0]["$jsonSchema"]["properties"]
    out = env.build_envelopes(rail=rail, payment_oid=OID)
    for name, block in out.items():
        assert set(block) == set(props[name]["properties"]), f"{name} shape drifted from spec"


# --- wire ---------------------------------------------------------------------

def test_wire_constants_and_derived_ref():
    w = env.build_envelopes(rail="WIRE", payment_oid=OID)["wireDetails"]
    assert w["messageDefinitionIdentifier"] == "pain.001.001.09"
    assert w["paymentMethod"] == "TRF"
    assert w["paymentInformationId"] == "PMTINF-7890abcd"


@pytest.mark.parametrize(
    "debtor_country,creditor_country,expected",
    [("US", "US", "DOMESTIC"), ("US", "GB", "INTERNATIONAL"), ("US", None, None), (None, None, None)],
)
def test_wire_type_derivation(debtor_country, creditor_country, expected):
    assert env.derive_wire_type(debtor_country, creditor_country) == expected


def test_supplied_wire_type_wins_over_derivation():
    w = env.build_wire_details(
        payment_oid=OID,
        debtor_bank_country="US",
        creditor_bank_country="US",
        supplied={"wireType": "INTERNATIONAL"},
    )
    assert w["wireType"] == "INTERNATIONAL"


def test_wire_routing_fields_are_null_at_initiation():
    """`network` and localInstrument.code are stage-4 routing decisions."""
    w = env.build_wire_details(payment_oid=OID, supplied={"paymentTypeInformation": {}})
    assert w["network"] is None
    assert w["paymentTypeInformation"]["localInstrument"]["code"] is None


def test_wire_parties_normalise_to_the_two_field_shape():
    w = env.build_wire_details(
        payment_oid=OID, supplied={"ultimateCreditor": {"name": "Acme Holdings"}}
    )
    assert w["ultimateCreditor"] == {"name": "Acme Holdings", "identification": None}
    assert w["ultimateDebtor"] == {"name": None, "identification": None}


# --- ach ----------------------------------------------------------------------

def test_ach_defaults_are_phase_one_placeholders():
    assert env.build_ach_details() == {"secCode": "PPD", "direction": "CREDIT"}


def test_ach_supplied_sec_code_wins():
    assert env.build_ach_details(supplied={"secCode": "CCD"})["secCode"] == "CCD"


# --- internal -----------------------------------------------------------------

@pytest.mark.parametrize("is_own_account,expected", [(True, "OWN_ACCOUNT"), (False, "THIRD_PARTY")])
def test_internal_transfer_type_from_shared_customer(is_own_account, expected):
    out = env.build_internal_details(is_own_account=is_own_account)
    assert out["transferType"] == expected


def test_internal_posting_reference_is_never_set_at_initiation():
    """FK into ledgerEvents — written asynchronously by the ledger service."""
    out = env.build_internal_details(is_own_account=True, supplied={"transferType": "THIRD_PARTY"})
    assert out["postingReference"] is None
    assert out["transferType"] == "THIRD_PARTY"
