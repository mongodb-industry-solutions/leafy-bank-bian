"""A minimal in-memory stand-in for the handful of pymongo calls stage 6 makes.

Deliberately small: it supports only the operations the code under test issues, so an
unsupported call fails loudly rather than silently passing. `raise_on` makes a named
collection's write fail, which is how the rollback tests force a failure.
"""

from __future__ import annotations

from typing import Any, Optional


def _matches(doc: dict, query: dict) -> bool:
    for key, want in query.items():
        have = doc
        for part in key.split("."):
            if not isinstance(have, dict) or part not in have:
                return False
            have = have[part]
        if isinstance(want, dict):
            if "$in" in want and have not in want["$in"]:
                return False
            if "$ne" in want and have == want["$ne"]:
                return False
            if "$gt" in want and not have > want["$gt"]:
                return False
            if "$exists" in want:
                return False          # not needed by any caller yet
        elif have != want:
            return False
    return True


def _set_path(doc: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        doc = doc.setdefault(part, {})
    doc[parts[-1]] = value


class _FakeCursor:
    """Enough of a pymongo cursor for `.find(...).sort(...).limit(...)` to read naturally in
    production code without the service having to know it is being faked."""

    def __init__(self, docs: list[dict]):
        self._docs = docs

    def sort(self, key, direction=1):
        self._docs.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        return self

    def limit(self, n: int):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)

    def __len__(self):
        return len(self._docs)


class FakeCollection:
    def __init__(self, name: str, owner: "FakeConnection"):
        self.name = name
        self.docs: list[dict] = []
        self._owner = owner

    # --- reads ---
    def find_one(self, query: dict, projection=None, sort=None, session=None) -> Optional[dict]:
        hits = [d for d in self.docs if _matches(d, query)]
        if sort:
            for key, direction in reversed(sort):
                hits.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        return dict(hits[0]) if hits else None

    def find(self, query: dict = None, projection=None, session=None):
        return _FakeCursor([dict(d) for d in self.docs if _matches(d, query or {})])

    def count_documents(self, query: dict, session=None) -> int:
        return len([d for d in self.docs if _matches(d, query)])

    def aggregate(self, pipeline, session=None):
        raise AssertionError(
            f"FakeCollection({self.name}).aggregate is not implemented — a test that needs it "
            "should stub the caller instead of relying on a fake pipeline engine."
        )

    # --- writes ---
    def _guard(self) -> None:
        if self._owner.raise_on == self.name:
            raise RuntimeError(f"forced failure writing {self.name}")

    def insert_one(self, doc: dict, session=None):
        self._guard()
        self._owner.journal.append(("insert_one", self.name))
        self.docs.append(dict(doc))
        return type("R", (), {"inserted_id": doc.get("_id")})()

    def insert_many(self, docs: list[dict], session=None):
        self._guard()
        self._owner.journal.append(("insert_many", self.name))
        self.docs.extend(dict(d) for d in docs)
        return type("R", (), {"inserted_ids": [d.get("_id") for d in docs]})()

    def update_many(self, query: dict, update: dict, session=None):
        self._guard()
        self._owner.journal.append(("update_many", self.name))
        n = 0
        for d in self.docs:
            if _matches(d, query):
                for k, v in update.get("$set", {}).items():
                    _set_path(d, k, v)
                n += 1
        return type("R", (), {"matched_count": n, "modified_count": n})()

    def update_one(self, query: dict, update: dict, session=None, upsert=False):
        self._guard()
        self._owner.journal.append(("update_one", self.name))
        for d in self.docs:
            if _matches(d, query):
                for k, v in update.get("$set", {}).items():
                    _set_path(d, k, v)
                for k, v in update.get("$push", {}).items():
                    d.setdefault(k.split(".")[-1] if "." not in k else k, None)
                    target = d
                    parts = k.split(".")
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target.setdefault(parts[-1], []).append(v)
                return type("R", (), {"matched_count": 1, "modified_count": 1})()
        return type("R", (), {"matched_count": 0, "modified_count": 0})()


class _FakeSession:
    def __init__(self, owner): self._owner = owner
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def start_transaction(self):
        owner = self._owner
        class _Txn:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, exc_type, *a):
                if exc_type is not None:
                    owner.rolled_back = True
                    owner._restore()
                return False
        owner._snapshot()
        return _Txn()


class _FakeClient:
    def __init__(self, owner): self._owner = owner
    def start_session(self): return _FakeSession(self._owner)
    def __getitem__(self, db_name): return self._owner


class FakeConnection:
    """Stands in for MongoDBConnection. One flat namespace of collections per instance."""

    def __init__(self, raise_on: Optional[str] = None):
        self.collections: dict[str, FakeCollection] = {}
        self.raise_on = raise_on
        self.journal: list[tuple[str, str]] = []
        self.rolled_back = False
        self.client = _FakeClient(self)
        self._saved: dict[str, list[dict]] = {}

    def get_collection(self, db_name: str, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection(name, self))

    def seed(self, name: str, docs: list[dict]) -> FakeCollection:
        coll = self.get_collection("db", name)
        coll.docs.extend(dict(d) for d in docs)
        return coll

    # Transaction emulation: snapshot every collection on entry, restore on failure. Crude,
    # but it is exactly the property the rollback tests assert — an aborted transaction
    # leaves nothing behind.
    def _snapshot(self) -> None:
        self._saved = {n: [dict(d) for d in c.docs] for n, c in self.collections.items()}

    def _restore(self) -> None:
        for name, docs in self._saved.items():
            self.collections[name].docs = docs
