"""The per-check audit trail every stage from 2 on appends to (`CHECK` docs in `paymentHistory`).

Originally the embedded array `payments.checks[]`; moved out with `lifecycle.events[]` because
both grew without bound (see `lifecycle_history.py`). The rationale below still holds.

## Why this array exists

`payments` had no field for any check outcome, and that was a decision, not an oversight:
`propose_payments.json:26` records that `approvals[]` was prototyped in review and
**reverted**. D2 and D3 later restored `lifecycle{}` and `refs{}`; `approvals[]` never came
back. Doc 07 §3.2 names the consequence — entitlement result "none", dual-authorization
result "none" — and doc 07's P1 item 4 asks for exactly this artifact: *per-check outcome,
actor, timestamp, sync/async*, for stage 2 **and** stage 3.

One general array rather than a per-stage structure (doc 15 B3): stage 2 appends its six
entries, stage 3 appends its ten, and a consumer filters by `stage`. The trace is one query on `paymentHistory` (`lifecycle_history.attach`).

## The rules

* **Append-only.** Never rewritten, never reordered — same contract as `lifecycle.events[]`.
  A correction is a new entry.
* Nothing here touches `status` or `lifecycle` — a check is evidence, not a state change.
  Stage 2 records six checks and moves the payment nowhere (doc 15 B5).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from contexts.payment_order_initiation.domain import lifecycle_history as history

logger = logging.getLogger(__name__)

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
    field: Optional[str] = None,
    code: Optional[str] = None,
    actor: str = "transactions-service",
    at: Optional[datetime] = None,
) -> dict:
    """Build one `checks[]` entry. Pure — no clock unless it has to reach for one.

    `at` is accepted so a stage recording several checks can stamp them all with one
    instant, which is what makes the array's order meaningful rather than incidental.

    `field` and `code` discharge FR-3.9 — on a validation failure the outcome is a
    structured triple (offending field, machine-readable failure code, human message)
    rather than a single boolean. They are `None` for non-FAIL outcomes (a PASS/WARN/SKIP
    has no offending field); a reader filters `result == "FAIL"` to see the structured
    failures. `field` is the dotted path on the payment instruction (e.g.
    `creditor.bic`); `code` is a stable identifier a consumer can switch on (e.g.
    `IDENTIFIER_FORMAT`), independent of the prose in `detail`.
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
        "field": field,
        "code": code,
        "at": at or datetime.now(timezone.utc),
        "actor": actor,
    }


def append_checks(payments, payment_oid, entries, *, session=None) -> None:
    """Record `entries` as `CHECK` documents in `paymentHistory`. A no-op for an empty list.

    One transaction: `$inc lifecycle.checkCount` by N on the payment, then insert the N
    documents with the `seq` range that increment allocated. Concurrent appends to one payment
    serialise on that `$inc`, so `seq` stays contiguous. `updatedAt` moves with the write — a
    check is a modification of the payment record even though it changes no state.

    Deliberately separate from `lifecycle.advance`: stage 2 records checks and performs
    **no** transition, so the two cannot be assumed to travel together.
    """
    if not entries:
        return
    entries = list(entries)

    def work(s):
        updated = payments.find_one_and_update(
            {"_id": payment_oid},
            {
                "$inc": {"lifecycle.checkCount": len(entries)},
                "$set": {"updatedAt": datetime.now(timezone.utc)},
            },
            session=s,
            return_document=True,
        )
        if updated is None:
            # Same tolerance as the old `update_one`: a check recorded against a payment that
            # is gone must not replace the business error the caller is about to raise.
            logger.warning("cannot record %d checks: payment %s not found", len(entries), payment_oid)
            return
        history.collection(payments).insert_many(
            history.check_documents(updated, entries), session=s)

    history.run_in_transaction(payments, session, work)


def stamp_validation_summary(payments, payment_oid, recorded: list) -> None:
    """FR-3.9 — write the `validation{}` summary snapshot: `overallStatus` + `failureReasons[]`.

    A summary of the per-check trail in `checks[]` (which stays the append-only detail).
    `overallStatus` is FAILED if any recorded check failed, PENDING if any is unresolved,
    else PASSED. `failureReasons[]` is the FAIL entries' `{field, code, detail}` — the
    structured triple FR-3.9 names, lifted out of the per-check array into a summary.

    Call this BEFORE `append_checks`/`_flush` clears `recorded`, in both the success path
    and a `refuse` (so a rejection still records FAILED + the reason). Enrichment's
    final-validation re-stamps it with its own verdict — `validation{}` holds the latest
    validation pass's outcome. Dotted `$set` paths, so `validation.determinedCategory`
    (and any sibling) survives.
    """
    failures = [c for c in recorded if c["result"] == FAIL]
    if failures:
        overall = "FAILED"
    elif any(c["result"] == PENDING for c in recorded):
        overall = "PENDING"
    else:
        overall = "PASSED"
    payments.update_one(
        {"_id": payment_oid},
        {"$set": {
            "validation.overallStatus": overall,
            "validation.failureReasons": [
                {"field": c["field"], "code": c["code"], "detail": c["detail"]}
                for c in failures
            ],
        }},
    )
