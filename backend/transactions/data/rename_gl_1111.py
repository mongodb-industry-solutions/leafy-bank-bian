"""One-time rename: GL 1111 "Nostro Accounts" -> "Nostro/Central Bank Cash" (plan-doina-oct6-fixes P1/D6).

Run from ``backend/transactions``::

    python -m data.rename_gl_1111            # dry run: show what it would change
    python -m data.rename_gl_1111 --apply    # rename

Only ``glAccounts.accountName`` for ``accountCode`` "1111" changes. 1121 (Minimum Reserve
Requirements) is untouched. Re-running is a no-op once the name matches.

The database is shared and the code carries the new name, so run this when the code deploys,
or the UI shows both names.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv

from database.connection import MongoDBConnection

logger = logging.getLogger(__name__)

ACCOUNT_CODE = "1111"
NEW_NAME = "Nostro/Central Bank Cash"


def rename(glaccounts, *, apply: bool) -> int:
    """Rename GL 1111. Returns the exit code: 1 when the account is missing, else 0."""
    account = glaccounts.find_one({"accountCode": ACCOUNT_CODE})
    if account is None:
        print(f"error: no glAccounts document with accountCode {ACCOUNT_CODE}.")
        return 1
    current = account.get("accountName")
    if current == NEW_NAME:
        print(f"{ACCOUNT_CODE} is already named {NEW_NAME!r}. Nothing to do.")
        return 0
    print(f"{ACCOUNT_CODE}: {current!r} -> {NEW_NAME!r}")
    if not apply:
        print("\nDry run. Re-run with --apply to rename.")
        return 0
    glaccounts.update_one({"accountCode": ACCOUNT_CODE}, {"$set": {"accountName": NEW_NAME}})
    print("Renamed.")
    return 0


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="rename; default is a dry run")
    args = parser.parse_args(argv)

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise SystemExit("MONGODB_URI is not set. Create backend/transactions/.env (see README).")
    connection = MongoDBConnection(uri)
    glaccounts = connection.get_collection(
        os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian"), "glAccounts")
    sys.exit(rename(glaccounts, apply=args.apply))


if __name__ == "__main__":
    main()
