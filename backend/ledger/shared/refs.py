"""Reference-derivation helpers for the ledger service.

Mirrors `leafy-bank-backend-transactions/backend/shared/refs.py` — same `derive_ref`
formula so typed string refs never drift across services. The ledger domain adds its
own prefixes for the FinancialAccounting / FinancialBookingLog collections.
"""

from __future__ import annotations

from bson import ObjectId

# Ledger-domain ref prefixes (BIAN FinancialAccounting / FinancialBookingLog).
#   LE-  ledgerEvents      (one accounting fact, one leg)
#   SL-  subLedgerEntries  (one subsidiary-ledger row, 1:1 with an event leg)
#   JNL- journalEntries    (one balanced summary journal per batch window + period)
#   GRP- groupId           (groups the debit + credit legs of one transaction within a ledgerEvent)
PREFIX_LEDGER_EVENT = "LE"
PREFIX_SUBLEDGER_ENTRY = "SL"
PREFIX_JOURNAL_ENTRY = "JNL"
PREFIX_GROUP = "GRP"


def derive_ref(prefix: str, oid: ObjectId | str, last_n: int = 8) -> str:
    """Build a typed string ref from an ObjectId.

    >>> derive_ref("JNL", ObjectId("661a4e0583b3a4567890abcd"))
    'JNL-7890abcd'
    """
    return f"{prefix}-{str(oid)[-last_n:]}"