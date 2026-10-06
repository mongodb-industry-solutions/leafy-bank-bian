"""A small in-memory stand-in for the pymongo surface the agent uses.

Supports exact-equality and dotted-path matching, `$in`/`$nin`, `$lt`/`$lte`/`$gt`/`$gte`,
`$exists`, `None` matching a missing field (MongoDB semantics — defect 2026-09-30),
`$set`/`$push` (with `$each`/`$slice`) with dotted paths, find().sort().limit(), count_documents, insert_one (with
optional unique keys raising DuplicateKeyError), update_many and find_one_and_update. Aggregations are not emulated; tests that need them patch the
`recon_evidence` function instead.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

_MISSING = object()


def _get(doc, path):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _match_value(val, cond):
    if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
        v = None if val is _MISSING else val
        for op, arg in cond.items():
            if op == "$in" and v not in arg:
                return False
            if op == "$nin" and v in arg:
                return False
            if op == "$exists" and (val is not _MISSING) != bool(arg):
                return False
            if op == "$gt" and (v is None or not v > arg):
                return False
            if op == "$ne" and v == arg:
                return False
            if op == "$lte" and (v is None or not v <= arg):
                return False
            if op == "$lt" and (v is None or not v < arg):
                return False
            if op == "$gte" and (v is None or not v >= arg):
                return False
        return True
    if cond is None:
        return val is _MISSING or val is None
    return val == cond


def matches(doc, query):
    for key, cond in query.items():
        if key == "$or":
            if not any(matches(doc, q) for q in cond):
                return False
            continue
        if not _match_value(_get(doc, key), cond):
            return False
    return True


def _set(doc, path, value):
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        if cur.get(part) is None:
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


class _Cursor(list):
    def sort(self, key, direction=1):
        return _Cursor(sorted(self, key=lambda d: d.get(key) or 0, reverse=direction < 0))

    def limit(self, n):
        return _Cursor(self[:n])


class FakeColl:
    def __init__(self, docs=None, unique=None):
        self.docs = [copy.deepcopy(d) for d in (docs or [])]
        # Each entry is a tuple of field paths; an optional `partial` query scopes it.
        self.unique = list(unique or [])

    def find_one(self, q=None, proj=None, **_):
        return next((copy.deepcopy(d) for d in self.docs if matches(d, q or {})), None)

    def find(self, q=None, proj=None):
        return _Cursor(copy.deepcopy(d) for d in self.docs if matches(d, q or {}))

    def count_documents(self, q=None):
        return sum(1 for d in self.docs if matches(d, q or {}))

    def _check_unique(self, doc, skip=None):
        for keys, partial in ((u[0], u[1]) if isinstance(u[0], tuple) else (u, None)
                              for u in self.unique):
            if partial is not None and not matches(doc, partial):
                continue
            key = tuple(_get(doc, k) for k in keys)
            for other in self.docs:
                if other is skip or (partial is not None and not matches(other, partial)):
                    continue
                if tuple(_get(other, k) for k in keys) == key:
                    raise DuplicateKeyError(f"duplicate key {dict(zip(keys, key))}")

    def insert_one(self, doc):
        self._check_unique(doc)
        doc.setdefault("_id", len(self.docs) + 1)
        self.docs.append(copy.deepcopy(doc))
        return SimpleNamespace(inserted_id=doc["_id"])

    @staticmethod
    def _apply(d, upd):
        for k, v in (upd.get("$set") or {}).items():
            _set(d, k, v)
        for k, v in (upd.get("$push") or {}).items():
            cur = _get(d, k)
            items = v["$each"] if isinstance(v, dict) and "$each" in v else [v]
            merged = ([] if cur is _MISSING or cur is None else cur) + list(items)
            if isinstance(v, dict) and "$slice" in v:
                merged = merged[v["$slice"]:] if v["$slice"] < 0 else merged[:v["$slice"]]
            _set(d, k, merged)

    def update_one(self, q, upd, upsert=False):
        for d in self.docs:
            if matches(d, q):
                self._apply(d, upd)
                return SimpleNamespace(matched_count=1)
        if upsert:
            doc = {k: v for k, v in q.items() if not k.startswith("$")}
            self._apply(doc, upd)
            self.docs.append(doc)
        return SimpleNamespace(matched_count=0)

    def update_many(self, q, upd):
        hits = [d for d in self.docs if matches(d, q)]
        for d in hits:
            self._apply(d, upd)
        return SimpleNamespace(matched_count=len(hits), modified_count=len(hits))

    def find_one_and_update(self, q, upd, return_document=ReturnDocument.BEFORE, **_):
        for d in self.docs:
            if matches(d, q):
                before = copy.deepcopy(d)
                candidate = copy.deepcopy(d)
                self._apply(candidate, upd)
                self._check_unique(candidate, skip=d)
                self._apply(d, upd)
                return copy.deepcopy(d) if return_document == ReturnDocument.AFTER else before
        return None


class FakeDB(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = FakeColl()
        return dict.__getitem__(self, name)
