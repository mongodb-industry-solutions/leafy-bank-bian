"""The GL 1111 rename script: dry-run default, idempotent, touches only 1111."""

from data.rename_gl_1111 import NEW_NAME, rename
from tests.test_payments_service import FakeCollection


def _glaccounts():
    return FakeCollection([
        {"accountCode": "1111", "accountName": "Nostro Accounts"},
        {"accountCode": "1121", "accountName": "Minimum Reserve Requirements"},
    ], key="accountCode")


def _name(coll, code):
    return coll.find_one({"accountCode": code})["accountName"]


def test_a_dry_run_changes_nothing():
    coll = _glaccounts()
    assert rename(coll, apply=False) == 0
    assert _name(coll, "1111") == "Nostro Accounts"


def test_apply_renames_1111_only_and_is_idempotent():
    coll = _glaccounts()
    assert rename(coll, apply=True) == 0
    assert _name(coll, "1111") == NEW_NAME
    assert _name(coll, "1121") == "Minimum Reserve Requirements"
    assert rename(coll, apply=True) == 0
    assert _name(coll, "1111") == NEW_NAME


def test_a_missing_account_fails_the_run():
    assert rename(FakeCollection([], key="accountCode"), apply=True) == 1
