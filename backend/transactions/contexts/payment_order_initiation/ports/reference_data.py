"""The reference-data port — stage 3 enrichment's only way to reach a lookup table.

Doc 17 §3 step 1. This module is **pure**: no pymongo, no FastAPI, no I/O, no clock.
`domain/` may import it; `domain/` may never import `adapters/` (doc 09 §"The one rule
that makes it work"). The Mongo implementation lives in
`adapters/mongo_reference_data.py` and is injected by `PaymentsService`.

Why a port at all: doc 12 §3 — *"Stage 3 grows from six fields to sixty; that only stays
testable if the dependency points inward."* Enrichment is graded by unit tests with
`InMemoryReferenceData`; the adapter is graded separately against a seeded collection.

## The three lookups, and why only three

- `bank_by_bic` — the creditor's bank identity (R13) and its clearing member id (R14).
- `bank_by_clearing_member` — the reverse, for a payment that arrives with an ABA but no
  BIC. Kept because Doina's `wire_domestic` sample carries both, and either may be the
  one the caller supplied.
- `purpose_code` — validates a supplied code against the table (R16). Her wording is
  *"tracked via a formal reference table … not just a free-text field"*, which is
  satisfied by the code being checked against a table rather than trusted.

Deliberately absent: any *search*. Semantic purpose-code matching over
`purposeCodes.embedding` is deferred (doc 17 §6) — it needs an embedding model inside the
synchronous payment path. Adding it later adds one method here and changes no caller.

## A miss is not an error

Every lookup returns `None` on a miss and raises nothing. Stage 3's enrichment half never
refuses (doc 17 B7): a missing directory row becomes a WARN check, because one absent seed
row must not fail every wire in the demo. That is the 2026-07-01 lesson — distinguish a
permanent data gap from a reason to stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class BankRecord:
    """A resolved financial institution, as stage 3 needs it.

    Field names mirror the `payments` agent sub-document (`debtor.*` / `creditor.*`) so
    enrichment copies across without a translation table — the mapping IS the name.
    Everything but `bic` is optional: a directory row may be partial, and a partial row is
    still better than nothing.
    """

    bic: str
    bank_name: Optional[str] = None
    bank_country: Optional[str] = None
    clearing_system_code: Optional[str] = None
    clearing_system_member_id: Optional[str] = None
    city: Optional[str] = None


@dataclass(frozen=True)
class PurposeCodeRecord:
    """A row of `purposeCodes`. `code` is the ISO 20022 ExternalPurpose value."""

    code: str
    name: str
    description: str
    category: Optional[str] = None


class ReferenceData(Protocol):
    """What stage 3 needs from the reference-data store. Implementations must not raise."""

    def bank_by_bic(self, bic: str) -> Optional[BankRecord]:
        ...

    def bank_by_clearing_member(
        self, clearing_system_code: str, member_id: str
    ) -> Optional[BankRecord]:
        ...

    def purpose_code(self, code: str) -> Optional[PurposeCodeRecord]:
        ...


class NullReferenceData:
    """Resolves nothing. The safe default when no store is configured.

    A service constructed without reference data still runs the whole saga: enrichment
    records WARN on every lookup and the payment proceeds. That keeps the reference-data
    store from becoming a hard dependency of the money path, and it is what makes the
    `ReferenceData` parameter safe to default.
    """

    def bank_by_bic(self, bic: str) -> Optional[BankRecord]:
        return None

    def bank_by_clearing_member(
        self, clearing_system_code: str, member_id: str
    ) -> Optional[BankRecord]:
        return None

    def purpose_code(self, code: str) -> Optional[PurposeCodeRecord]:
        return None


class InMemoryReferenceData:
    """A real implementation over dicts, for unit tests and fixtures.

    Lives here rather than in `tests/` on purpose: it is pure, it is the reference
    behaviour the Mongo adapter must match, and a test that builds its own fake is a test
    that can drift from the port (defect 2026-08-31 — a consumer graded against a fixture
    no producer could produce). The adapter is still tested separately against a seeded
    collection; this class is not a substitute for that.

    Lookups are case-insensitive on BIC and purpose code, matching the adapter, because
    callers upper-case inconsistently and a directory miss caused by casing is the least
    useful failure available.
    """

    def __init__(
        self,
        banks: Optional[list[BankRecord]] = None,
        purpose_codes: Optional[list[PurposeCodeRecord]] = None,
    ) -> None:
        self._by_bic = {b.bic.upper(): b for b in (banks or [])}
        self._by_member = {
            (b.clearing_system_code, b.clearing_system_member_id): b
            for b in (banks or [])
            if b.clearing_system_code and b.clearing_system_member_id
        }
        self._purpose = {p.code.upper(): p for p in (purpose_codes or [])}

    def bank_by_bic(self, bic: str) -> Optional[BankRecord]:
        if not bic:
            return None
        return self._by_bic.get(bic.upper())

    def bank_by_clearing_member(
        self, clearing_system_code: str, member_id: str
    ) -> Optional[BankRecord]:
        if not clearing_system_code or not member_id:
            return None
        return self._by_member.get((clearing_system_code, member_id))

    def purpose_code(self, code: str) -> Optional[PurposeCodeRecord]:
        if not code:
            return None
        return self._purpose.get(code.upper())
