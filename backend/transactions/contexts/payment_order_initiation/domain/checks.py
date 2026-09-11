"""`payments.checks[]` — the per-check audit trail every stage from 2 on appends to.

## Why this array exists

`payments` had no field for any check outcome, and that was a decision, not an oversight:
`propose_payments.json:26` records that `approvals[]` was prototyped in review and
**reverted**. D2 and D3 later restored `lifecycle{}` and `refs{}`; `approvals[]` never came
back. Doc 07 §3.2 names the consequence — entitlement result "none", dual-authorization
result "none" — and doc 07's P1 item 4 asks for exactly this artifact: *per-check outcome,
actor, timestamp, sync/async*, for stage 2 **and** stage 3.

One general array rather than a per-stage structure (doc 15 B3): stage 2 appends its six
entries, stage 3 appends its ten, and a consumer filters by `stage`. The demo trace is
then a single document read, which is the point D2 made when it embedded `lifecycle{}`.

## The rules

* **Append-only.** Never rewritten, never reordered — same contract as `lifecycle.events[]`.
  A correction is a new entry.
* **Not `required`, and nullable.** Added the way D2/D3 added their blocks, so documents
  written before this change stay valid and a reader must tolerate the array being absent
  rather than `[]`. Doina ratifies it as Q12; the D2/D3 precedent is to build and then ratify.
* Nothing here touches `status` or `lifecycle` — a check is evidence, not a state change.
  Stage 2 records six checks and moves the payment nowhere (doc 15 B5).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# `result` vocabulary. PENDING exists for an ASYNC check whose answer has not arrived;
# stage 2's checks are all SYNC, so it goes unused until a later stage needs it.
PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
PENDING = "PENDING"
# WARN — the check ran, found something worth surfacing, and did NOT stop the payment.
# Added in stage 3 (doc 17 B4/B7) for the two outcomes that are neither a pass nor a
# refusal: a content-duplicate (suspicious, not invalid) and a reference-data miss (our
# directory is thin, not the caller's error). Distinct from SKIP, which means the check did
# not run at all — collapsing the two would make an unresolved beneficiary indistinguishable
# from one nobody looked at.
WARN = "WARN"

RESULTS = (PASS, FAIL, SKIP, PENDING, WARN)

# Doina asks that every check declare whether it is synchronous or asynchronous (R11), from
# stage 2 onward — it is what tells a reader whether a missing answer is a failure or a wait.
SYNC = "SYNC"
ASYNC = "ASYNC"

MODES = (SYNC, ASYNC)


def check(
    stage: str,
    name: str,
    result: str,
    *,
    mode: str = SYNC,
    detail: str = "",
    actor: str = "transactions-service",
    at: Optional[datetime] = None,
) -> dict:
    """Build one `checks[]` entry. Pure — no clock unless it has to reach for one.

    `at` is accepted so a stage recording several checks can stamp them all with one
    instant, which is what makes the array's order meaningful rather than incidental.
    """
    if result not in RESULTS:
        raise ValueError(f"unknown check result {result!r}")
    if mode not in MODES:
        raise ValueError(f"unknown check mode {mode!r}")
    return {
        "stage": stage,
        "name": name,
        "result": result,
        "mode": mode,
        "detail": detail,
        "at": at or datetime.now(timezone.utc),
        "actor": actor,
    }


def append_checks(payments, payment_oid, entries, *, session=None) -> None:
    """Append entries to `payments.checks[]`. A no-op for an empty list.

    `$push`/`$each` creates the array when it is absent, so no `$exists` branch and no
    migration of existing documents. `updatedAt` moves with the write — a check is a
    modification of the payment record even though it changes no state.

    Deliberately not folded into `lifecycle.advance`'s `extra`: stage 2 records checks and
    performs **no** transition, so the two cannot be assumed to travel together. A stage
    that does both may still pass its checks through `advance(extra=...)` instead, and get
    one write.
    """
    if not entries:
        return
    payments.update_one(
        {"_id": payment_oid},
        {
            "$push": {"checks": {"$each": list(entries)}},
            "$set": {"updatedAt": datetime.now(timezone.utc)},
        },
        session=session,
    )
