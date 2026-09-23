"""Mongo implementation of the `ReferenceData` port. Doc 17 §3 step 1.

Reads two reference collections, **never writes them**. Seeding is a hand-run operation
(`backend/data/load_reference_seed.py`), deliberately not something the service does at
startup: these collections live in a database shared with other demos, so an app-managed
write is how you corrupt someone else's data.

## Two collections

- **`correspondentBanks`** — BIC-indexed institution directory. Stage 3 reads records with
  `recordType: "BIC_DIRECTORY"` (new; see doc 17 B2 and Q22). The canonical spec's
  existing record types are `JP_ENTITY_BANK` / `IN_BRANCH`, both corridor-specific with
  no generic clearing-member fields, so the directory rows carry
  `clearingSystemCode` / `clearingSystemMemberId` — two fields the spec does not yet
  declare. **Filtered by `recordType` on every query** so JP/IN rows belonging to another
  demo can never be returned as a US or European bank.
- **`purposeCodes`** — ISO 20022 ExternalPurpose code table, `code` unique.

## Projections are allowlists, not exclusions

Both collections may be shared. Defect 2026-08-31 (`performance`): an exclusion list stops
working the moment another demo attaches a fat field — and `purposeCodes` in particular
carries a 1024-dim `embedding` that must never come back over the wire for a code lookup.
Every projection here names the fields the port returns and nothing else.

## Misses, and errors

A miss returns `None`. A *driver* error also returns `None`, logged at warning: enrichment
is best-effort by design (doc 17 B7), so a reference-data outage degrades to WARN checks
rather than failing payments. This is the only place that decision is implemented; the
domain never sees an exception from a lookup.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pymongo.errors import PyMongoError

from contexts.payment_order_initiation.ports.reference_data import (
    BankRecord,
    PurposeCodeRecord,
)

logger = logging.getLogger(__name__)

# The directory record type stage 3 owns. JP/IN rows are another demo's and are excluded.
BIC_DIRECTORY = "BIC_DIRECTORY"

_BANK_FIELDS = {
    "_id": 0,
    "swiftCode": 1,
    "bankName": 1,
    "country": 1,
    "clearingSystemCode": 1,
    "clearingSystemMemberId": 1,
    "city": 1,
}

_PURPOSE_FIELDS = {"_id": 0, "code": 1, "name": 1, "description": 1, "category": 1}


class MongoReferenceData:
    """`ReferenceData` over `correspondentBanks` + `purposeCodes`."""

    def __init__(self, db: Any) -> None:
        # The database handle, not the collection handles. Resolved per lookup so
        # constructing this adapter touches nothing: a service that never enriches never
        # reaches for a reference collection, and a database that has none still yields a
        # working (empty-resolving) store.
        self._db = db

    def _collection(self, name: str) -> Optional[Any]:
        """The named collection, or None if this database cannot supply it.

        Real pymongo is lazy and always returns a handle. Other database-like objects
        (notably test fakes) may not, and a database with no reference data seeded is a
        legitimate state — it resolves nothing, exactly like `NullReferenceData`.
        """
        try:
            return self._db[name]
        except (KeyError, TypeError):
            logger.debug("no %s collection on this database — resolving nothing", name)
            return None

    # --- banks ---------------------------------------------------------------

    def bank_by_bic(self, bic: str) -> Optional[BankRecord]:
        if not bic:
            return None
        return self._bank({"recordType": BIC_DIRECTORY, "swiftCode": bic.upper()})

    def bank_by_clearing_member(
        self, clearing_system_code: str, member_id: str
    ) -> Optional[BankRecord]:
        if not clearing_system_code or not member_id:
            return None
        return self._bank(
            {
                "recordType": BIC_DIRECTORY,
                "clearingSystemCode": clearing_system_code,
                "clearingSystemMemberId": member_id,
            }
        )

    def _bank(self, query: dict) -> Optional[BankRecord]:
        doc = self._find_one(
            self._collection("correspondentBanks"), query, _BANK_FIELDS,
            "correspondentBanks",
        )
        if not doc or not doc.get("swiftCode"):
            # A row with no BIC cannot satisfy the port's contract (`bic` is required on
            # BankRecord), so treat it as a miss rather than fabricating one.
            return None
        return BankRecord(
            bic=doc["swiftCode"],
            bank_name=doc.get("bankName"),
            bank_country=doc.get("country"),
            clearing_system_code=doc.get("clearingSystemCode"),
            clearing_system_member_id=doc.get("clearingSystemMemberId"),
            city=doc.get("city"),
        )

    # --- purpose codes -------------------------------------------------------

    def purpose_code(self, code: str) -> Optional[PurposeCodeRecord]:
        if not code:
            return None
        doc = self._find_one(
            self._collection("purposeCodes"), {"code": code.upper()}, _PURPOSE_FIELDS,
            "purposeCodes",
        )
        if not doc:
            return None
        return PurposeCodeRecord(
            code=doc["code"],
            # `name` and `description` are `required` in the spec, but this database is
            # shared and the rows may not be ours — degrade rather than KeyError.
            name=doc.get("name", ""),
            description=doc.get("description", ""),
            category=doc.get("category"),
        )

    # --- shared --------------------------------------------------------------

    @staticmethod
    def _find_one(
        collection: Optional[Any], query: dict, projection: dict, label: str
    ) -> Optional[dict]:
        """One projected read. Never raises — see the module docstring."""
        if collection is None:
            return None
        try:
            return collection.find_one(query, projection)
        except PyMongoError:
            logger.warning(
                "reference-data lookup failed on %s (%s) — treating as unresolved",
                label,
                query,
                exc_info=True,
            )
            return None
