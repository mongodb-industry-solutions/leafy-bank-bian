"""Shared test isolation for the payment-agent service."""

from __future__ import annotations

import pytest

import semantic


@pytest.fixture(autouse=True)
def _no_voyage(monkeypatch):
    """Tests never call Voyage, whatever VOYAGE_API_KEY the shell carries. A test that wants
    semantic results stubs `semantic.embed` / `semantic.rerank` itself."""
    monkeypatch.setattr(semantic, "_client", lambda: None)
