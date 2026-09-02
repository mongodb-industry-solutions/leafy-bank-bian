"""Stage 3 — duplicate detection. Doc 17 §3 step 4 (R4), decision B4.

Doina L435 pairs *"Duplicate detection and idempotency"*, and her **Core Journey Flow 2** is
*"API attempts to enter a duplicate payment … the operations analyst retries or repairs it."*

## Three verbs, and only one of them is ours

- **Detect** — this module. Stage 3's.
- **Enforce** (refuse the second financial effect) — the `idx_idempotency_key_unique` index,
  deliberately deferred to the end of the stage sequence.
- **Repair / retry** — stage 9, in her own words.

⚠️ **This module must never cause a caller to send an idempotency key.** `_state.md` carries a
standing precondition on the deferred index: until it exists on Atlas, a `find_one` pre-check
would dedupe *sequential* retries and look correct, while two *concurrent* requests with the
same key would both move money — a failure that appears only under real concurrency. Detection
here is content-based and advisory, so it touches none of that. Step 4's gate greps that no
caller sends `Idempotency-Key`.

## Why WARN and not refusal

Two identical $25,000 supplier payments ten minutes apart are **suspicious, not invalid**.
`endToEndId` and `uetr` are unique per request, so there is no true identity to key on — only
a resemblance. Refusing on resemblance would make legitimate repeat payments impossible; a
demo that silently blocks them teaches the wrong lesson. So a match records
`duplicate_detection` as WARN with the matched `paymentId` in `detail`, and the payment
proceeds. That also gives stage 9 a real signal to queue.

## What counts as a resemblance

Same debtor account, same beneficiary, same instructed amount, same currency, inside a
window (default 15 minutes, `DUPLICATE_WINDOW_SECONDS`). Terminal-failure states are excluded:
a duplicate of a payment that was REJECTED produced no financial effect, so resembling it is
not a warning worth raising.

Pure except for the env read. The query is *built* here and *executed* by the caller, so the
predicate stays unit-testable without a database.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

# 15 minutes. Long enough to catch a double-submit or an impatient retry, short enough that a
# genuine second invoice payment later the same day is not flagged. Env-overridable so a demo
# can be retuned without a deploy.
_DEFAULT_WINDOW_SECONDS = 900

# States in which no money moved, so a resemblance is not worth reporting.
TERMINAL_FAILURE_STATES = ("REJECTED", "FAILED", "CANCELLED")


def window_seconds() -> int:
    """The look-back window, in seconds.

    Read **lazily**, never at module scope: a module-level `os.getenv` runs before
    `load_dotenv` and silently takes the default (defects.md 2026-06-30). Same discipline as
    stage 2's entitlement thresholds.
    """
    raw = os.getenv("DUPLICATE_WINDOW_SECONDS")
    if raw is None:
        return _DEFAULT_WINDOW_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_WINDOW_SECONDS
    return value if value > 0 else _DEFAULT_WINDOW_SECONDS


def recent_duplicate_filter(
    *,
    debtor_account_id: str,
    creditor_account_no: Optional[str],
    instructed_amount: float,
    instructed_currency: str,
    now: datetime,
    exclude_payment_id: str,
) -> dict:
    """The Mongo filter for a content-duplicate of this payment.

    `exclude_payment_id` is not optional in practice: the instruction is persisted at DRAFT in
    stage 1 *before* this stage runs (doc 11 §11), so without it every payment matches itself
    and every payment warns.

    Leads on `debtor.accountId`, which `idx_payments_debtorAccount_status` indexes. No new
    index is added — at this collection's size a scan is sub-millisecond, and adding one
    without measuring is defect 2026-07-08.
    """
    return {
        "debtor.accountId": debtor_account_id,
        "creditor.accountNo": creditor_account_no,
        "instructedAmount": instructed_amount,
        "instructedCurrency": instructed_currency,
        "paymentId": {"$ne": exclude_payment_id},
        "status": {"$nin": list(TERMINAL_FAILURE_STATES)},
        "createdAt": {"$gte": now - timedelta(seconds=window_seconds())},
    }


def describe(match: dict, *, now: Optional[datetime] = None) -> str:
    """The `detail` string for a WARN, naming what it resembles and how long ago."""
    payment_id = match.get("paymentId", "unknown")
    created = match.get("createdAt")
    status = match.get("status", "unknown")

    when = ""
    if isinstance(created, datetime) and isinstance(now, datetime):
        minutes = max(0, int((now - created).total_seconds() // 60))
        when = f" {minutes} minute(s) ago" if minutes else " less than a minute ago"

    return (
        f"Resembles {payment_id}{when} (status {status}): same debtor account, "
        f"beneficiary, amount and currency within the last {window_seconds() // 60} "
        "minute(s). Not refused — a repeat payment can be legitimate, and no idempotency "
        "key was supplied to prove otherwise."
    )
