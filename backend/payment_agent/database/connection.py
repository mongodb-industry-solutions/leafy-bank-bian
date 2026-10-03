"""Minimal MongoDB connection wrapper for the payment-agent service.

The agent reads reference collections (`purposeCodes`, `correspondentBanks`) read-only and
uses Atlas as its own checkpointer store (`MongoDBSaver`). It never writes payment state —
that stays in the transactions service. Mirrors the sibling services' `MongoDBConnection`
shape so the same `MONGODB_URI` / `LEAFYBANK_DB_NAME` env vars are reused.
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo import MongoClient

logger = logging.getLogger(__name__)


class MongoDBConnection:
    def __init__(self, uri: str) -> None:
        self.client = MongoClient(uri)

    def get_database(self, db_name: str) -> Any:
        return self.client[db_name]

    def get_collection(self, db_name: str, collection_name: str) -> Any:
        return self.client[db_name][collection_name]

    def close(self) -> None:
        self.client.close()
