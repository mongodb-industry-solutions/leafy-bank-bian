#!/usr/bin/env python3
"""Queue hygiene: close every OPEN exception through its designed resolution route.

Precedent: the 10-02 gate dismissed 16 stale orphans the same way. Dry-run by
default; pass --apply to execute.

Per category, never chosen by hand:
  RECONCILIATION_DISCREPANCY -> POST_ADJUSTMENT when chargeBearer == DEBT, else
                                ACCEPT_DISCREPANCY. The bearer gate in
                                payments_service.resolve_exception refuses the
                                other combination, so this mirrors the only
                                legal closing action.
  ORPHANED_SETTLEMENT         -> LINK to its OPEN MISSING twin when that twin is
                                the agent's top candidate (the R2-pair shape
                                approved on 10-03); otherwise DISMISS.
  RECONCILIATION_MISSING     -> closed by its twin orphan's LINK (pass 1 plans
                                the LINK and marks the twin); leftovers print
                                for manual pairing, never auto-guessed.
  SETTLEMENT_UNMATCHED       -> ACCEPT_DISCREPANCY — RETURN_FUNDS moves money
                                and is never part of a cleanup.
  anything else              -> DISMISS.

ESCALATE_TO_CORRESPONDENT is never used: _escalate keeps the row OPEN (A4 D4) —
that is why the 10-03-approved R4 (PAY-bb089a16) is still in the queue.

Env overrides: TRANSACTIONS_BASE_URL (default http://localhost:8002),
LEDGER_BASE_URL (default http://localhost:8003).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

TXN = os.getenv("TRANSACTIONS_BASE_URL", "http://localhost:8002").rstrip("/")
LEDGER = os.getenv("LEDGER_BASE_URL", "http://localhost:8003").rstrip("/")

MISSING = "RECONCILIATION_MISSING"
DISCREPANCY = "RECONCILIATION_DISCREPANCY"
ORPHANED = "ORPHANED_SETTLEMENT"
UNMATCHED = "SETTLEMENT_UNMATCHED"

NOTE = "queue cleanup 2026-10-03 (backend/data/clean_exception_queue.py)"


def call(method: str, url: str, body: dict | None = None):
    """Return (status, parsed-body-or-error-detail). Never raises on HTTP errors."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", "")
        except Exception:
            detail = e.reason
        return e.code, detail


def open_exceptions() -> list[dict]:
    """GET /workflow/exceptions?status=OPEN returns `{"items": [...], "total": N}` where
    each item is a payment row (or a statement-line row for orphans) with the exception
    nested under `"exception"` (workflow_read_service.list_exceptions). The plan works
    on the exception docs, not the joined rows."""
    status, data = call("GET", f"{TXN}/workflow/exceptions?status=OPEN&limit=100")
    if status != 200:
        sys.exit(f"cannot list OPEN exceptions: {status} {data}")
    items = data.get("items", []) if isinstance(data, dict) else data
    return [r["exception"] for r in items if isinstance(r, dict) and r.get("exception")]


def top_candidate_pid(row: dict) -> str | None:
    candidates = (row.get("agent") or {}).get("candidates") or []
    return candidates[0].get("paymentId") if candidates else None


def bearer(payment_id: str) -> str | None:
    status, pay = call("GET", f"{TXN}/workflow/payments/{payment_id}")
    return pay.get("chargeBearer") if status == 200 else None


def plan(rows: list[dict], skip: set[str]) -> list[tuple[str, str, dict, str]]:
    """Two passes: MISSING rows first (their LINK closes the twin orphan too),
    then everything else. (route, verb, url, body, why) entries as tuples."""
    missing_by_pid = {r["paymentId"]: r for r in rows
                       if r.get("category") == MISSING and r.get("paymentId")}
    handled: set[str] = set()
    steps = []

    for r in rows:
        if r.get("category") != MISSING or r.get("paymentId") in skip:
            continue
        # LINK from the twin orphan side: one call closes both twins.
        twin = next((o for o in rows
                     if o.get("category") == ORPHANED
                     and top_candidate_pid(o) == r["paymentId"]
                     and o.get("exceptionId") not in handled), None)
        if twin is not None:
            steps.append((twin.get("exceptionId"), "ledger-link",
                          {"paymentId": r["paymentId"], "note": NOTE},
                          f"orphan {twin.get('paymentId')} links MISSING twin {r['paymentId']} — both close"))
            handled.add(twin["exceptionId"])
        else:
            steps.append((r["exceptionId"], "MANUAL", {},
                          f"MISSING {r['paymentId']}: no OPEN twin orphan — pair by hand "
                          f"(POST {LEDGER}/pipeline/exceptions/{r['exceptionId']}/link "
                          "needs paymentMessageId + lineNo)"))

    for r in rows:
        eid, pid, cat = r.get("exceptionId"), r.get("paymentId"), r.get("category")
        if not eid:
            print(f"  MANUAL  row without exceptionId skipped: {json.dumps(r)[:200]}")
            continue
        if eid in handled or pid in skip or eid in skip:
            continue
        if cat == DISCREPANCY:
            b = bearer(pid)
            if b is None:
                steps.append((eid, "MANUAL", {}, f"DISCREPANCY {pid}: payment fetch failed"))
                continue
            action = "POST_ADJUSTMENT" if b == "DEBT" else "ACCEPT_DISCREPANCY"
            steps.append((eid, "txn-resolve", {"action": action, "note": NOTE},
                          f"{pid} bearer={b} -> {action}"))
        elif cat == ORPHANED:
            twin_pid = top_candidate_pid(r)
            if twin_pid and twin_pid in missing_by_pid and twin_pid not in skip:
                steps.append((eid, "ledger-link", {"paymentId": twin_pid, "note": NOTE},
                              f"orphan {pid} links MISSING twin {twin_pid}"))
            else:
                steps.append((eid, "txn-resolve", {"action": "DISMISS", "note": NOTE},
                              f"{pid}: no OPEN MISSING twin -> dismiss"
                              + (f" (top candidate {twin_pid} has no OPEN MISSING)"
                                 if twin_pid else "")))
        elif cat == UNMATCHED:
            steps.append((eid, "txn-resolve", {"action": "ACCEPT_DISCREPANCY", "note": NOTE},
                          f"{pid} legacy UNMATCHED -> accept (no money moves)"))
        else:
            steps.append((eid, "txn-resolve", {"action": "DISMISS", "note": NOTE},
                          f"{cat} {pid} -> dismiss"))
    return steps


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    ap.add_argument("--skip", default="", help="comma-separated paymentIds/exceptionIds to leave alone")
    args = ap.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    rows = open_exceptions()
    print(f"{len(rows)} OPEN exceptions")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("category") or "(no category)"] = counts.get(r.get("category") or "(no category)", 0) + 1
    for cat, n in sorted(counts.items()):
        print(f"  {n:3} {cat}")
    for r in rows:
        if not r.get("paymentId"):
            print(f"  ⚠ row without paymentId: {r.get('exceptionId')} "
                  f"({r.get('category')}) — handled as MANUAL/DISMISS only")
    print()
    steps = plan(rows, skip)

    for eid, verb, body, why in steps:
        if verb == "MANUAL":
            print(f"  MANUAL  {eid}: {why}")
            continue
        if not args.apply:
            print(f"  would {verb:12} {eid}: {why}")
            continue
        if verb == "txn-resolve":
            status, out = call("POST", f"{TXN}/workflow/exceptions/{eid}/resolve", body)
        else:
            status, out = call("POST", f"{LEDGER}/pipeline/exceptions/{eid}/link", body)
        result = (out or {}).get("status") if isinstance(out, dict) else out
        print(f"  {status} {verb:12} {eid} -> {result}  ({why})")

    if args.apply:
        left = open_exceptions()
        print(f"\n{len(left)} OPEN exceptions remain")
        for r in left:
            print(f"  {r.get('exceptionId')} {r.get('category')} {r.get('paymentId')}")


if __name__ == "__main__":
    main()
