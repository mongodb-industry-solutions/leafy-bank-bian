"""The `cutoffWorkflow` collection: every throwaway record the cut-off demo needs.

One collection, three document kinds, told apart by `kind`:

  CLOCK             a demo clock run (`business_clock`); `_id` is `CLK-...`
  APPROVAL_REQUEST  an open dual-approval hold (`hold_queues`)
  SCREENING_ITEM    an open sanctions-screening hold (`hold_queues`)

`staffDirectory` stays separate: it is reference data, not a workflow record.

Records that should vanish carry `expireAt`; a single TTL index (expireAfterSeconds 0)
removes them. Documents without the field, the real approval requests, never expire.

The payment_agent mirrors these names in `cutoff_evidence.py` and `cutoff_clock.py`; the
two services run from separate venvs and cannot import each other.
"""

from __future__ import annotations

from datetime import datetime, timedelta

COLLECTION = "cutoffWorkflow"

CLOCK = "CLOCK"
APPROVAL_REQUEST = "APPROVAL_REQUEST"
SCREENING_ITEM = "SCREENING_ITEM"

TTL_SECONDS = 86400


def expires_at(created: datetime) -> datetime:
    """When a throwaway record is due to expire, on the wall clock."""
    return created + timedelta(seconds=TTL_SECONDS)
