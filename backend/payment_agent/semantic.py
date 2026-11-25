"""Voyage AI embeddings and reranking for the Reconciliation Agent.

Semantic retrieval is evidence, never a precondition: with no `VOYAGE_API_KEY`, or on any
Voyage error, every function returns None and callers fall back to their deterministic
path. The client is built lazily so the service and its tests boot without a key.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

EMBED_DIMENSIONS = 1024  # voyage-3.5 default; the vector index is built for this size


def embed_model() -> str:
    return os.getenv("VOYAGE_EMBED_MODEL", "voyage-3.5")


def rerank_model() -> str:
    return os.getenv("VOYAGE_RERANK_MODEL", "rerank-2.5")


@lru_cache(maxsize=1)
def _client():
    key = os.getenv("VOYAGE_API_KEY")
    if not key:
        logger.info("VOYAGE_API_KEY not set: semantic retrieval off.")
        return None
    import voyageai

    return voyageai.Client(api_key=key)


def enabled() -> bool:
    return _client() is not None


def embed(texts: list[str], *, query: bool) -> Optional[list[list[float]]]:
    """One vector per text. `query` picks Voyage's query or document input type."""
    client = _client()
    if client is None or not texts:
        return None
    try:
        out = client.embed(texts, model=embed_model(),
                           input_type="query" if query else "document",
                           output_dimension=EMBED_DIMENSIONS)
        return out.embeddings
    except Exception:  # noqa: BLE001 — degrade, never break an investigation
        logger.warning("Voyage embed failed", exc_info=True)
        return None


def rerank(query: str, documents: list[str]) -> Optional[list[float]]:
    """Relevance of each document to `query`, in the documents' order."""
    client = _client()
    if client is None or not query or not documents:
        return None
    try:
        out = client.rerank(query, documents, model=rerank_model())
    except Exception:  # noqa: BLE001
        logger.warning("Voyage rerank failed", exc_info=True)
        return None
    scores = [0.0] * len(documents)
    for r in out.results:
        scores[r.index] = round(float(r.relevance_score), 4)
    return scores
