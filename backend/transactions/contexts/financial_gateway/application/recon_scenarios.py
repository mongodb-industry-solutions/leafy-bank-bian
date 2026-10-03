"""The reconciliation scenario pack (plan B3): stage R1–R5 for the agent demo in one call.

Each wire goes through the real `initiate_payment` (the reachability lesson, defect
2026-09-01: an outcome is only demonstrable if the running system can produce it), then
`complete_due` settles them and one statement cycle books them with an orphan line.

| # | Wire | Statement lever | What the engine raises |
|---|---|---|---|
| R1  | GB, DEBT | FEE_DEDUCTED      | leg 2 MISMATCH $25 → RECONCILIATION_DISCREPANCY |
| R1b | GB, SHAR | FEE_DEDUCTED      | same $25, but the beneficiary bears it |
| R2  | DE       | REFERENCE_ALTERED | payment MISSING + the line ORPHANED (twins) |
| R3  | CH       | LATE              | MISSING once the window lapses; the next cycle heals it |
| R4  | CA       | AMOUNT_TRANSPOSED | leg 2 MISMATCH on swapped digits, no `Chrgs` |
| R5  | —        | injected orphan   | ORPHANED_SETTLEMENT |

The exceptions themselves are raised by the ledger's GL batch (statement matching + the
reconciliation sweep), not here. This only puts the facts in place.

## Why every amount is under 10,000

From the third wire on, one debtor inside the velocity window scores VELOCITY 18 +
cross-border 12 + new beneficiary 15 + baseline 4 = 49 — one point under REVIEW (50), pinned
by `test_no_scenario_wire_is_held_for_fraud_review_or_flagged_duplicate`. The 10,000 band
adds 5 and would hold the wire at PENDING_REVIEW instead of settling it. The amounts also
differ, so the duplicate-content check never warns, and each has two adjacent differing
whole-unit digits, so AMOUNT_TRANSPOSED produces a real swap.
"""

from __future__ import annotations

from typing import Optional

from contexts.financial_gateway.application.statement import (
    NOSTRO_USD, PRESENTER_DRIVEN_REF_PREFIX, generate_statement,
)
from contexts.financial_gateway.domain import camt053
from contexts.payment_settlement import settle

_BANKS = {
    "GB": {"bic": "BARCGB22", "bankName": "Barclays Bank PLC", "clearingSystemCode": "GBDSC",
           "clearingSystemMemberId": "202053",
           "name": "Northwind Traders Ltd", "address": "12 Cheapside, London"},
    "DE": {"bic": "DEUTDEFF", "bankName": "Deutsche Bank AG", "clearingSystemCode": "DEBLZ",
           "clearingSystemMemberId": "50070010",
           "name": "Fabrikam Logistics GmbH", "address": "Hafenstrasse 8, Hamburg"},
    "CH": {"bic": "UBSWCHZH", "bankName": "UBS Switzerland AG", "clearingSystemCode": "CHBCC",
           "clearingSystemMemberId": "230",
           "name": "Tailspin Aviation SA", "address": "Route de Meyrin 21, Geneva"},
    "CA": {"bic": "ROYCCAT2", "bankName": "Royal Bank of Canada", "clearingSystemCode": "CACPA",
           "clearingSystemMemberId": "000300002",
           "name": "Adventure Works Supply Co", "address": "77 Harbour Street, Toronto"},
}

SCENARIOS = (
    # key, country, chargeBearer, statement lever, amount, beneficiary account, purpose
    ("R1", "GB", "DEBT", camt053.FEE_DEDUCTED, 4_850.00, "GB29NWBK60161331926819", "Invoice settlement"),
    ("R1b", "GB", "SHAR", camt053.FEE_DEDUCTED, 3_275.00, "GB82WEST12345698765432", "Supplier payment"),
    ("R2", "DE", "SHAR", camt053.REFERENCE_ALTERED, 6_120.00, "DE89370400440532013000", "Freight and handling"),
    ("R3", "CH", "SHAR", camt053.LATE, 2_940.00, "CH9300762011623852957", "Consulting services"),
    ("R4", "CA", "SHAR", camt053.AMOUNT_TRANSPOSED, 7_360.00, "003000012345678", "Quarterly rent"),
)
TOTAL = sum(s[4] for s in SCENARIOS)

# Plan E — the walkthrough initiates one wire per click. R5 is a clean wire (its orphan line
# is injected at statement time); TL is a clean wire whose statement the presenter withholds,
# so the line is genuinely late. R3 stays batch-only: TL tells the timing story on demand.
WALKTHROUGH_SCENARIOS = {s[0]: s for s in SCENARIOS if s[0] != "R3"} | {
    "R5": ("R5", "GB", "SHAR", camt053.CLEAN, 5_630.00, "GB33BUKB20201555555555", "Software licences"),
    "TL": ("TL", "CH", "SHAR", camt053.CLEAN, 2_485.00, "CH5604835012345678009", "Advisory retainer"),
}


def _debtor(service, amount: float = TOTAL) -> dict:
    """A funded, active customer account to send from. Customer types only — the
    shared `accounts` collection also holds NOSTRO/GL accounts (defect 2026-06-29)."""
    account = service.accounts.find_one(
        {"type": {"$in": ["CURRENT", "SAVINGS", "CHECKING"]}, "status": "ACTIVE",
         "currency": "USD", "balance.available": {"$gte": amount}},
        {"accountId": 1, "customerSnapshot.customerId": 1},
    )
    if account is None:
        raise ValueError(f"No active USD customer account holds {amount:,.2f} to fund the scenarios.")
    return account


def _initiate(service, debtor: dict, scenario: tuple) -> dict:
    key, country, bearer, lever, amount, account_no, purpose = scenario
    bank = _BANKS[country]
    doc = service.initiate_payment(
        customer_ref=(debtor.get("customerSnapshot") or {}).get("customerId"),
        debtor_account_ref=debtor["accountId"],
        creditor_account_ref=None,
        creditor_party={"name": bank["name"], "accountNo": account_no, "bic": bank["bic"],
                        "bankName": bank["bankName"], "bankCountry": country,
                        "address": bank["address"],
                        "clearingSystemCode": bank["clearingSystemCode"],
                        "clearingSystemMemberId": bank["clearingSystemMemberId"]},
        instructed_amount=amount,
        instructed_currency="USD",
        payment_type="CREDIT_TRANSFER",
        payment_rail="WIRE",
        remittance_unstructured=purpose,
        charge_bearer=bearer,
        channel="BRANCH",
        client_reference=f"{PRESENTER_DRIVEN_REF_PREFIX}{key}",
        statement_outcome=lever,
    )
    return {"paymentId": doc["paymentId"], "status": doc["status"],
            "chargeBearer": bearer, "statementOutcome": lever, "amount": amount}


def run_one(service, key: str) -> dict:
    """Initiate one walkthrough scenario's wire. No settle, no statement: the presenter
    drives those steps. Raises KeyError for an unknown key, ValueError if nothing funds it."""
    scenario = WALKTHROUGH_SCENARIOS[key]
    bank = _BANKS[scenario[1]]
    payment = _initiate(service, _debtor(service, scenario[4]), scenario)
    return {"scenario": key, **payment, "bic": bank["bic"], "bankName": bank["bankName"],
            "includeOrphan": key == "R5"}


def run(service, connection, db_name: str, *, now=None) -> dict:
    """Initiate R1–R4, settle them, book one statement (+ the R5 orphan). Returns the ids."""
    debtor = _debtor(service)
    payments = {s[0]: _initiate(service, debtor, s) for s in SCENARIOS}

    settled = settle.complete_due(connection, db_name, delay_seconds=0)
    statement = generate_statement(service.db, account_code=NOSTRO_USD, include_orphan=True, now=now)
    orphan = next((e for e in (statement or {}).get("entries", []) if e.get("simulatedPaymentId") is None), None)
    return {
        "debtorAccountId": debtor["accountId"],
        "payments": payments,
        "settled": settled,
        "statement": None if statement is None else {
            "paymentMessageId": statement["paymentMessageId"],
            "sequence": statement["statement"]["sequence"],
            "entryCount": len(statement["entries"]),
        },
        "R5": None if orphan is None else {"paymentMessageId": statement["paymentMessageId"],
                                            "lineNo": orphan["lineNo"],
                                            "reference": orphan["reference"]},
    }


def advance_cycle(service, connection, db_name: str, *, account_code: str = NOSTRO_USD,
                  now=None) -> Optional[dict]:
    """One statement cycle on demand (settle what is due, then book it). R3's LATE line lands
    here. Matching and the sweep run on the ledger's GL batch, so trigger that next."""
    settle.complete_due(connection, db_name, delay_seconds=0)
    return generate_statement(service.db, account_code=account_code, include_orphan=False, now=now)
