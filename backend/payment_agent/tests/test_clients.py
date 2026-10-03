"""The service clients: route URLs, bodies, and 4xx → ServiceRefused."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import clients


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def sent(monkeypatch):
    log = []

    def urlopen(req, timeout=None):
        log.append((req.full_url, json.loads(req.data)))
        return _Resp(b'{"outcome": "RECONCILED"}')
    monkeypatch.setattr(clients.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("LEDGER_BASE_URL", "http://ledger")
    monkeypatch.setenv("TRANSACTIONS_BASE_URL", "http://txn/")
    return log


def test_routes_and_bodies(sent):
    clients.recheck("EXC-1")
    clients.link("EXC-1", payment_message_id="PM-1", line_no=2)
    clients.reconcile("PAY-1")
    clients.resolve("EXC-1", "POST_ADJUSTMENT", note="n")
    urls = [u for u, _ in sent]
    assert urls == ["http://ledger/pipeline/exceptions/EXC-1/recheck",
                    "http://ledger/pipeline/exceptions/EXC-1/link",
                    "http://ledger/pipeline/reconcile/PAY-1",
                    "http://txn/workflow/exceptions/EXC-1/resolve"]
    assert sent[1][1] == {"paymentId": None, "paymentMessageId": "PM-1", "lineNo": 2, "note": None}
    assert sent[3][1] == {"action": "POST_ADJUSTMENT", "note": "n"}


def test_a_4xx_is_a_refusal_not_a_crash(monkeypatch):
    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 422, "x", {}, io.BytesIO(b'{"detail": "not legal"}'))
    monkeypatch.setattr(clients.urllib.request, "urlopen", urlopen)
    with pytest.raises(clients.ServiceRefused) as e:
        clients.resolve("EXC-1", "ACCEPT_DISCREPANCY")
    assert e.value.status == 422 and e.value.detail == "not legal"
