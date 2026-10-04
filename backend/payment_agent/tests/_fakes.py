"""A small in-memory stand-in for the pymongo surface the agent uses.

Supports exact-equality and dotted-path matching, `$in`, `$lt`/`$lte`/`$gte`, `None` matching a
missing field (MongoDB semantics — defect 2026-09-30), `$set`/`$push` with dotted paths, and
find().sort().limit(). Aggregations are not emulated; tests that need them patch the
`recon_evidence` function instead.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

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
    def __init__(self, docs=None):
        self.docs = [copy.deepcopy(d) for d in (docs or [])]

    def find_one(self, q=None, proj=None, **_):
        return next((copy.deepcopy(d) for d in self.docs if matches(d, q or {})), None)

    def find(self, q=None, proj=None):
        return _Cursor(copy.deepcopy(d) for d in self.docs if matches(d, q or {}))

    def update_one(self, q, upd):
        for d in self.docs:
            if matches(d, q):
                for k, v in (upd.get("$set") or {}).items():
                    _set(d, k, v)
                for k, v in (upd.get("$push") or {}).items():
                    cur = _get(d, k)
                    _set(d, k, ([] if cur in (_MISSING, None) else cur) + [v])
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)


class FakeDB(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = FakeColl()
        return dict.__getitem__(self, name)
