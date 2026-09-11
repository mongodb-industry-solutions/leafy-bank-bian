"""Builds the three rail-specific initiation envelopes. Pure — no I/O, no clock.

Doina stage 1, requirement R4: `rail` is the sole discriminator. All three envelopes
are ALWAYS present on the document; the one matching the rail is populated and the
other two are all-null. The spec says so explicitly on each envelope
("Always present; all fields null when rail != X"), and it is what makes
`{"wireDetails.network": ...}` style queries indexable without an `$exists` branch.

Same contract as `payment_document.py`: pure, so the shape is testable with no database.

What stage 1 may derive, and what it may NOT
--------------------------------------------
Derived here (stage 1 genuinely knows these):
  * `messageDefinitionIdentifier` / `paymentMethod` — constants for a
    credit-transfer-initiated wire.
  * `paymentInformationId` — from the same ObjectId seed as every other ref.
  * `wireType` — DOMESTIC vs INTERNATIONAL is defined by the spec as a comparison of
    `debtor.bankCountry` and `creditor.bankCountry`, both captured at initiation.
    Doc 12 lists domestic-vs-cross-border as a stage-3 TODO; that is the *enrichment*
    of an unresolved case. The envelope field is stage 1, so it is derived here and
    stage 3 may refine it. When either country is unknown the value stays null rather
    than guessing — an invented wireType would route a payment.
  * `internalDetails.transferType` — OWN_ACCOUNT when debtor and creditor share a
    customer. `ctx.is_internal` already computes exactly that; note the name means
    "same customer", NOT "same bank".

Deliberately null, not derived:
  * `wireDetails.network` and `paymentTypeInformation.localInstrument.code` — routing
    decisions made in stage 4 (orchestration). The spec permits null at initiation.
  * `internalDetails.postingReference` — an FK into `ledgerEvents`, written
    asynchronously by the ledger service. Stage 1 cannot know it.

`achDetails` is a Phase-2 placeholder. It is built so the document shape is right, not
because ACH works.
"""

from __future__ import annotations

from typing import Optional

from shared.refs import derive_ref

WIRE = "WIRE"
ACH = "ACH"
INTERNAL = "INTERNAL"

_MESSAGE_DEFINITION_IDENTIFIER = "pain.001.001.09"
_PAYMENT_METHOD = "TRF"          # pain.001 PmtInf/PmtMtd — always TRF here
_DEFAULT_SEC_CODE = "PPD"        # Doina: "individuals only, for Phase 1"
_DEFAULT_ACH_DIRECTION = "CREDIT"


def _null_party() -> dict:
    """pain.001 UltmtDbtr / UltmtCdtr / InitgPty — the same two-field shape."""
    return {"name": None, "identification": None}


def _party(supplied: Optional[dict]) -> dict:
    if not supplied:
        return _null_party()
    return {
        "name": supplied.get("name"),
        "identification": supplied.get("identification"),
    }


def _payment_type_information(supplied: Optional[dict]) -> dict:
    supplied = supplied or {}
    service_level = supplied.get("serviceLevel") or {}
    local_instrument = supplied.get("localInstrument") or {}
    return {
        "serviceLevel": {"code": service_level.get("code")},
        "localInstrument": {
            # Stage 4 sets this; network identity lives in `network`, not here.
            "code": local_instrument.get("code"),
            "proprietary": local_instrument.get("proprietary"),
        },
    }


def derive_wire_type(
    debtor_bank_country: Optional[str], creditor_bank_country: Optional[str]
) -> Optional[str]:
    """DOMESTIC when both agents sit in the same country, INTERNATIONAL when they
    differ, null when either side is unknown. Never guesses — see module docstring."""
    if not debtor_bank_country or not creditor_bank_country:
        return None
    return "DOMESTIC" if debtor_bank_country == creditor_bank_country else "INTERNATIONAL"


def null_wire_details() -> dict:
    return {
        "messageDefinitionIdentifier": None,
        "wireType": None,
        "network": None,
        "paymentInformationId": None,
        "paymentMethod": None,
        "paymentTypeInformation": None,
        "ultimateDebtor": None,
        "ultimateCreditor": None,
        "initiatingParty": None,
    }


def null_ach_details() -> dict:
    return {"secCode": None, "direction": None}


def null_internal_details() -> dict:
    return {"transferType": None, "postingReference": None}


def build_wire_details(
    *,
    payment_oid,
    debtor_bank_country: Optional[str] = None,
    creditor_bank_country: Optional[str] = None,
    supplied: Optional[dict] = None,
) -> dict:
    """The pain.001 initiation envelope. A caller-supplied `wireType` wins over the
    derived one — the customer's own DOMESTIC/INTERNATIONAL declaration is an input,
    not something to override."""
    supplied = supplied or {}
    return {
        "messageDefinitionIdentifier": _MESSAGE_DEFINITION_IDENTIFIER,
        "wireType": supplied.get("wireType")
        or derive_wire_type(debtor_bank_country, creditor_bank_country),
        "network": None,  # stage 4 (orchestration)
        "paymentInformationId": derive_ref("PMTINF", payment_oid),
        "paymentMethod": _PAYMENT_METHOD,
        "paymentTypeInformation": _payment_type_information(
            supplied.get("paymentTypeInformation")
        ),
        "ultimateDebtor": _party(supplied.get("ultimateDebtor")),
        "ultimateCreditor": _party(supplied.get("ultimateCreditor")),
        "initiatingParty": _party(supplied.get("initiatingParty")),
    }


def build_ach_details(*, supplied: Optional[dict] = None) -> dict:
    """Phase-2 placeholder. The effective entry date is NOT here — it derives from the
    common-layer `requestedExecutionDate`."""
    supplied = supplied or {}
    return {
        "secCode": supplied.get("secCode") or _DEFAULT_SEC_CODE,
        "direction": supplied.get("direction") or _DEFAULT_ACH_DIRECTION,
    }


def build_internal_details(
    *, is_own_account: bool, supplied: Optional[dict] = None
) -> dict:
    supplied = supplied or {}
    return {
        "transferType": supplied.get("transferType")
        or ("OWN_ACCOUNT" if is_own_account else "THIRD_PARTY"),
        "postingReference": None,  # FK into ledgerEvents, written by the ledger service
    }


def build_envelopes(
    *,
    rail: str,
    payment_oid,
    is_own_account: bool = False,
    debtor_bank_country: Optional[str] = None,
    creditor_bank_country: Optional[str] = None,
    wire_details: Optional[dict] = None,
    ach_details: Optional[dict] = None,
    internal_details: Optional[dict] = None,
) -> dict:
    """All three envelopes, exactly one populated. Merge straight into the payment
    document. Rails with no envelope of their own (CARD, RTP) get three null ones —
    their detail lives in the top-level `cardTxn` / `rtp` blocks."""
    envelopes = {
        "wireDetails": null_wire_details(),
        "achDetails": null_ach_details(),
        "internalDetails": null_internal_details(),
    }

    if rail == WIRE:
        envelopes["wireDetails"] = build_wire_details(
            payment_oid=payment_oid,
            debtor_bank_country=debtor_bank_country,
            creditor_bank_country=creditor_bank_country,
            supplied=wire_details,
        )
    elif rail == ACH:
        envelopes["achDetails"] = build_ach_details(supplied=ach_details)
    elif rail == INTERNAL:
        envelopes["internalDetails"] = build_internal_details(
            is_own_account=is_own_account, supplied=internal_details
        )

    return envelopes
