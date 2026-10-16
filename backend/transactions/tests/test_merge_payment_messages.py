"""The paymentMessages -> canonicalJsonStorage copy script: dry-run default, never touches
existing target documents, idempotent, drops the source only when fully copied."""

from data.merge_payment_messages import merge


class _Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.dropped = False

    def count_documents(self, flt, **kw):
        if "_id" in flt:
            return sum(1 for d in self.docs if d["_id"] == flt["_id"])
        return len(self.docs)

    def find(self, flt, proj=None):
        ids = flt.get("_id", {}).get("$in")
        return [dict(d) for d in self.docs if ids is None or d["_id"] in ids]

    def insert_many(self, docs, **kw):
        self.docs.extend(docs)

    def drop(self):
        self.dropped = True


def _pair():
    source = _Coll([{"_id": "a", "paymentMessageId": "PM-1"},
                    {"_id": "b", "paymentMessageId": "PM-2"}])
    target = _Coll([{"_id": "demo-uuid", "conversionId": "MT103_to_JSON"}])
    return source, target


def test_a_dry_run_changes_nothing():
    source, target = _pair()
    assert merge(source, target, apply=False) == 0
    assert len(target.docs) == 1 and not source.dropped


def test_apply_copies_and_leaves_the_demos_documents_alone():
    source, target = _pair()
    assert merge(source, target, apply=True) == 0
    assert {d["_id"] for d in target.docs} == {"demo-uuid", "a", "b"}
    assert target.docs[0] == {"_id": "demo-uuid", "conversionId": "MT103_to_JSON"}
    assert not source.dropped


def test_a_second_run_copies_nothing():
    source, target = _pair()
    merge(source, target, apply=True)
    assert merge(source, target, apply=True) == 0
    assert len(target.docs) == 3


def test_drop_source_runs_only_after_a_full_copy():
    source, target = _pair()
    assert merge(source, target, apply=True, drop_source=True) == 0
    assert source.dropped


def test_drop_source_is_refused_when_the_target_is_missing_documents():
    source, target = _pair()
    target.insert_many = lambda docs, **kw: None  # a copy that silently loses everything
    assert merge(source, target, apply=True, drop_source=True) == 1
    assert not source.dropped
