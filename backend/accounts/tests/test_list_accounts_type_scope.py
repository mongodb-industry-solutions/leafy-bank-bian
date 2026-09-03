"""`list_accounts` must not leak bank-internal accounts into a directory query.

Stage 7 (doc 21 B1) seeds NOSTRO clearing accounts into `accounts`, because the spec's
`required` list omits `customerId` and both `_money_move` and `_principal_gl_account`
need an `accounts` document with a `gl.accountCode`. Those rows are legitimate, and they
are not payment counterparties — so an unfiltered `CurrentAccount/Request` must not return
them.

Hermetic on purpose: the behaviour under test is the **query** `list_accounts` builds, not
what Mongo does with it, so a recording stub is both sufficient and faster than the live
cluster the sibling suite needs. Doc 21 step 0's gate.

The field names here come from `accounts_service` itself (`customerSnapshot.customerId`
at the `_owned_accounts` and `list_accounts` call sites), not from the spec's prose —
defect 2026-09-02, where a fixture invented `transactionId` for a collection storing
`txnId` and the test passed on the same misreading as the code.
"""

from __future__ import annotations

import pytest

from services.accounts_service import CUSTOMER_FACING_ACCOUNT_TYPES, AccountsService


class _RecordingCollection:
    """Captures the filter it was called with and returns nothing."""

    def __init__(self) -> None:
        self.last_query: dict | None = None

    def find(self, query, *args, **kwargs):
        self.last_query = query
        return iter(())


@pytest.fixture
def service_and_collection():
    svc = AccountsService.__new__(AccountsService)  # bypass __init__'s DB connection
    coll = _RecordingCollection()
    svc.accounts = coll
    return svc, coll


def test_an_unfiltered_request_asks_only_for_customer_facing_types(service_and_collection):
    svc, coll = service_and_collection

    svc.list_accounts({})

    assert coll.last_query["type"] == {"$in": list(CUSTOMER_FACING_ACCOUNT_TYPES)}
    assert "NOSTRO" not in coll.last_query["type"]["$in"]


def test_an_explicit_type_still_reaches_bank_internal_accounts(service_and_collection):
    """The settlement UI reads a clearing position by asking for NOSTRO directly."""
    svc, coll = service_and_collection

    svc.list_accounts({"type": "NOSTRO"})

    assert coll.last_query["type"] == "NOSTRO"


def test_the_customer_and_status_filters_still_compose(service_and_collection):
    svc, coll = service_and_collection

    svc.list_accounts({"customerId": "CUST-00000001", "status": "ACTIVE"})

    assert coll.last_query["customerSnapshot.customerId"] == "CUST-00000001"
    assert coll.last_query["status"] == "ACTIVE"
    # Still scoped — a customer query must not pick up a clearing account either.
    assert coll.last_query["type"] == {"$in": list(CUSTOMER_FACING_ACCOUNT_TYPES)}


def test_the_type_scope_matches_the_eod_topup_workers_own_filter():
    """The two are deliberate duplicates; this is what makes the drift fail loudly.

    `eod_topup_worker.py` keeps its pair as a literal so the balance-corrupting path does
    not depend on an import from `services/` (defect 2026-06-29). A duplicate with no
    equality assertion is exactly the mirror-drift this repo keeps logging.
    """
    import inspect

    from workers import eod_topup_worker

    source = inspect.getsource(eod_topup_worker.run_once)
    for account_type in CUSTOMER_FACING_ACCOUNT_TYPES:
        assert f'"{account_type}"' in source, (
            f"{account_type} is customer-facing in accounts_service but absent from "
            "eod_topup_worker's filter — the two have drifted"
        )
