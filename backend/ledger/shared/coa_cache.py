"""In-memory Chart of Accounts.

The CoA (``glAccounts``) is small, slow-changing reference data. This service loads it once at
startup and passes it to the (pure, I/O-free) posting rules — never read it from the DB inside a
transaction.

``ChartOfAccounts`` is a thin, immutable lookup keyed on ``accountCode`` (the FK every
other collection joins on). A CoA change requires a service restart — acceptable for the
demo; documented as such.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # avoid importing pymongo (via connection) for pure in-memory use / tests
    from database.connection import MongoDBConnection


class ChartOfAccounts:
    """Read-only accountCode -> account-document index."""

    def __init__(self, accounts: list[dict]):
        self._by_code: dict[str, dict] = {a["accountCode"]: a for a in accounts}

    @classmethod
    def from_db(cls, connection: "MongoDBConnection", db_name: str) -> "ChartOfAccounts":
        """Load every glAccounts document into memory."""
        coll = connection.get_collection(db_name, "glAccounts")
        return cls(list(coll.find({})))

    def get(self, account_code: str) -> Optional[dict]:
        return self._by_code.get(account_code)

    def exists(self, account_code: str) -> bool:
        return account_code in self._by_code

    def is_posting_account(self, account_code: str) -> bool:
        """True only for leaf accounts that accept journal entries."""
        acct = self._by_code.get(account_code)
        return bool(acct and acct.get("isPostingAccount"))

    def require_posting_account(self, account_code: str) -> dict:
        """Return the account, or raise if it is missing / not a postable leaf.

        Postings may only land on ``isPostingAccount: true`` leaves (resolved #2).
        """
        acct = self._by_code.get(account_code)
        if acct is None:
            raise ValueError(f"GL account {account_code!r} not found in chart of accounts")
        if not acct.get("isPostingAccount"):
            raise ValueError(f"GL account {account_code!r} is not a posting account (not a leaf)")
        return acct

    def require_active_posting_account(self, account_code: str) -> dict:
        """Return the account, or raise if missing, not a posting leaf, or not ACTIVE."""
        acct = self.require_posting_account(account_code)
        if acct.get("status") != "ACTIVE":
            raise ValueError(
                f"GL account {account_code!r} is not ACTIVE (status={acct.get('status')!r})"
            )
        return acct

    def control_account_for(self, account_code: str) -> str:
        """Return the control account a posting leaf rolls up to.

        The control account is the nearest ancestor with isPostingAccount=false — in this CoA
        the level-3 '— Control' account directly above a level-4 leaf. Used to key subLedger /
        GL summarization (design Step 3/5: glAccountCode=leaf, controlAccountCode=control).
        """
        acct = self._by_code.get(account_code)
        if acct is None:
            raise ValueError(f"GL account {account_code!r} not found in chart of accounts")
        seen: set[str] = set()
        cur = acct
        while True:
            parent_code = cur.get("parentAccountCode")
            if not parent_code:
                raise ValueError(f"no control (non-posting) ancestor for {account_code!r}")
            if parent_code in seen:
                raise ValueError(f"CoA hierarchy cycle at {parent_code!r}")
            seen.add(parent_code)
            parent = self._by_code.get(parent_code)
            if parent is None:
                raise ValueError(f"parent {parent_code!r} of {account_code!r} not in chart of accounts")
            if not parent.get("isPostingAccount"):
                return parent_code
            cur = parent

    def require_active_control_account(self, account_code: str) -> dict:
        """Return the account if it exists and is ACTIVE; assert it is a control (non-posting) account.

        Design Step 5.5: controlAccountCode must exist + be ACTIVE; isPostingAccount=false is
        expected. Raises on missing, inactive, or (defensively) on a posting leaf.
        """
        acct = self._by_code.get(account_code)
        if acct is None:
            raise ValueError(f"GL account {account_code!r} not found in chart of accounts")
        if acct.get("status") != "ACTIVE":
            raise ValueError(f"GL account {account_code!r} is not ACTIVE (status={acct.get('status')!r})")
        if acct.get("isPostingAccount"):
            raise ValueError(f"GL account {account_code!r} is a posting leaf, not a control account")
        return acct

    def __len__(self) -> int:
        return len(self._by_code)

    def __contains__(self, account_code: object) -> bool:
        return account_code in self._by_code