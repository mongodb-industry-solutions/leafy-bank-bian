"""Reference-data access for the Enrichment Agent.

Reads two shared collections on `fsi-bian-test-db` (read-only — seeding is another team's
job, see `repo-is-not-the-database`):

- `correspondentBanks` (BIC-indexed institution directory, `recordType == "BIC_DIRECTORY"`)
- `purposeCodes` (ISO 20022 ExternalPurpose table, 1024-d voyage-3 `embedding` per row)

Two lookups back the agent's tools:

- `bank_by_bic` — resolve a creditor BIC to bank name / country / clearing member id.
- `purpose_codes_semantic` — `$vectorSearch` the customer's free-text remittance purpose
  against `embedding`, return the top-k candidates with cosine score. The agent reasons over
  the candidates; the transactions service's deterministic validator (FR-3.11) still decides
  whether the chosen code is legal.

## Projections are allowlists

Both collections are shared across demos. Every query names the fields it returns and nothing
else — the 1024-d `embedding` never crosses the wire for a lookup (defect 2026-08-31
`performance`).

## Never raises

A miss, a driver error, an unconfigured embedder, or an embedding-provider outage all return
`None` / `[]`. The agent is best-effort by design (doc 17 B7): a reference-data outage
degrades to "no candidates proposed", never a failed payment — the transactions service
treats an empty proposal list as "agent had nothing to add" and proceeds deterministically.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)

BIC_DIRECTORY = "BIC_DIRECTORY"

# Atlas Vector Search index on `purposeCodes.embedding` (1024-d, cosine, voyage-3).
# Pre-exists on `fsi-bian-test-db` — owned by the team that seeded the embeddings
# (confirmed 2026-09-24: name `purposeCodesVector`, status READY). We reference it; we do
# not create or manage it.
PURPOSE_CODES_VECTOR_INDEX = "purposeCodesVector"

# voyage-3: 1024-d. Must match the model that produced `purposeCodes.embedding` — the 15
# codes were seeded by the `fsi-payments-processing` demo, which uses `voyage-3`
# (`fsi-payments-processing/backend/payment_agent/config/settings.py:36`). Same 1024-d
# as voyage-4, but a DIFFERENT model — embedding across models is near-random
# cosine (~0.5), which is why the agent saw weak matches (~0.49) and proposed nothing.
VOYAGE_MODEL = "voyage-3"
VOYAGE_DIM = 1024

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

_PURPOSE_SEMANTIC_FIELDS = {
    "_id": 0,
    "code": 1,
    "name": 1,
    "description": 1,
    "category": 1,
    "score": {"$meta": "vectorSearchScore"},
}


@dataclass(frozen=True)
class BankRecord:
    bic: str
    bank_name: Optional[str] = None
    bank_country: Optional[str] = None
    clearing_system_code: Optional[str] = None
    clearing_system_member_id: Optional[str] = None
    city: Optional[str] = None


@dataclass(frozen=True)
class PurposeCodeRecord:
    code: str
    name: str
    description: str
    category: Optional[str] = None


@dataclass(frozen=True)
class PurposeCodeMatch:
    """A semantic match from `purpose_codes_semantic` with cosine score (0–1)."""

    code: str
    name: str
    description: str
    category: Optional[str] = None
    score: float = 0.0


class Embedder(Protocol):
    def embed(self, text: str) -> Optional[list[float]]:
        ...


class VoyageEmbedder:
    """`Embedder` over the `voyageai` client. No-op when `VOYAGE_API_KEY` is unset."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or os.getenv("VOYAGE_API_KEY")
        self._client = None
        if not self._api_key:
            logger.info(
                "VoyageEmbedder: VOYAGE_API_KEY unset — semantic lookup will propose no "
                "candidates (graceful miss)."
            )
            return
        try:
            import voyageai  # lazy: a missing package doesn't break import-time
        except ImportError:  # pragma: no cover - env-dependent
            logger.warning(
                "VoyageEmbedder: `voyageai` not installed despite VOYAGE_API_KEY — "
                "semantic lookup disabled."
            )
            return
        try:
            self._client = voyageai.Client(api_key=self._api_key)
        except Exception:  # noqa: BLE001
            logger.warning(
                "VoyageEmbedder: voyageai.Client() failed — semantic lookup disabled.",
                exc_info=True,
            )

    def embed(self, text: str) -> Optional[list[float]]:
        if not self._client or not text:
            return None
        try:
            result = self._client.embed(texts=[text], model=VOYAGE_MODEL)
            vector = result.embeddings[0] if result.embeddings else None
            if vector is None:
                return None
            if len(vector) != VOYAGE_DIM:
                logger.warning(
                    "VoyageEmbedder: model %s returned dim %d, expected %d — dropping vector.",
                    VOYAGE_MODEL,
                    len(vector),
                    VOYAGE_DIM,
                )
                return None
            return list(vector)
        except Exception:  # noqa: BLE001
            logger.warning(
                "VoyageEmbedder: embed() failed for query %r — returning no vector.",
                text[:80],
                exc_info=True,
            )
            return None


class ReferenceData:
    """Read-only reference-data store backing the Enrichment Agent's tools."""

    def __init__(self, db: Any, embedder: Optional[Embedder] = None) -> None:
        self._db = db
        self._embedder = embedder

    def _collection(self, name: str) -> Optional[Any]:
        try:
            return self._db[name]
        except (KeyError, TypeError):
            logger.debug("no %s collection on this database — resolving nothing", name)
            return None

    def bank_by_bic(self, bic: str) -> Optional[BankRecord]:
        if not bic:
            return None
        doc = self._find_one(
            self._collection("correspondentBanks"),
            {"recordType": BIC_DIRECTORY, "swiftCode": bic.upper()},
            _BANK_FIELDS,
            "correspondentBanks",
        )
        if not doc or not doc.get("swiftCode"):
            return None
        return BankRecord(
            bic=doc["swiftCode"],
            bank_name=doc.get("bankName"),
            bank_country=doc.get("country"),
            clearing_system_code=doc.get("clearingSystemCode"),
            clearing_system_member_id=doc.get("clearingSystemMemberId"),
            city=doc.get("city"),
        )

    def purpose_code(self, code: str) -> Optional[PurposeCodeRecord]:
        if not code:
            return None
        doc = self._find_one(
            self._collection("purposeCodes"),
            {"code": code.upper()},
            _PURPOSE_FIELDS,
            "purposeCodes",
        )
        if not doc:
            return None
        return PurposeCodeRecord(
            code=doc["code"],
            name=doc.get("name", ""),
            description=doc.get("description", ""),
            category=doc.get("category"),
        )

    def purpose_codes_semantic(
        self, query_text: str, k: int = 3
    ) -> list[PurposeCodeMatch]:
        if not query_text or not self._embedder or k <= 0:
            return []
        try:
            vector = self._embedder.embed(query_text)
        except Exception:  # noqa: BLE001
            logger.warning(
                "embedding failed for query %r — no candidates", query_text[:80],
                exc_info=True,
            )
            return []
        if not vector:
            return []
        collection = self._collection("purposeCodes")
        if collection is None:
            return []
        pipeline = [
            {
                "$vectorSearch": {
                    "index": PURPOSE_CODES_VECTOR_INDEX,
                    "path": "embedding",
                    "queryVector": vector,
                    "numCandidates": max(10 * k, 50),
                    "limit": k,
                }
            },
            {"$project": _PURPOSE_SEMANTIC_FIELDS},
        ]
        try:
            return [
                PurposeCodeMatch(
                    code=doc.get("code", ""),
                    name=doc.get("name", ""),
                    description=doc.get("description", ""),
                    category=doc.get("category"),
                    score=float(doc.get("score", 0.0)),
                )
                for doc in collection.aggregate(pipeline)
            ]
        except PyMongoError:
            logger.warning(
                "purpose-code $vectorSearch failed — no candidates", exc_info=True,
            )
            return []

    @staticmethod
    def _find_one(
        collection: Optional[Any], query: dict, projection: dict, label: str
    ) -> Optional[dict]:
        if collection is None:
            return None
        try:
            return collection.find_one(query, projection)
        except PyMongoError:
            logger.warning(
                "reference-data lookup failed on %s (%s) — unresolved",
                label, query, exc_info=True,
            )
            return None
