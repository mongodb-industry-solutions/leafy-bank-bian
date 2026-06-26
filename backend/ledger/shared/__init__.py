"""Shared primitives for the ledger service.

Unlike the BIAN operational services (accounts / transactions), the ledger domain is
camelCase end-to-end on the wire (post-2026-05-05 decision), so there is no BIAN alias
registry here — only the ref helpers and (added in Step C) the posting rules + CoA cache.
"""

from .refs import (
    PREFIX_GROUP,
    PREFIX_JOURNAL_ENTRY,
    PREFIX_LEDGER_EVENT,
    PREFIX_SUBLEDGER_ENTRY,
    derive_ref,
)

__all__ = [
    "derive_ref",
    "PREFIX_LEDGER_EVENT",
    "PREFIX_SUBLEDGER_ENTRY",
    "PREFIX_JOURNAL_ENTRY",
    "PREFIX_GROUP",
]