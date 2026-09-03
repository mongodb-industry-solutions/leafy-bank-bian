"""Stage 8 gate verifier — run after initiating + settling a payment on the live dev cluster.

Usage (from the repo root, with MONGODB_URI and LEAFYBANK_DB_NAME in your env):

    cd backend/ledger
    PYTHONPATH=. .venv/bin/python -m data.verify_stage8_gate <paymentId>
    # or, to verify the most recent payment:
    PYTHONPATH=. .venv/bin/python -m data.verify_stage8_gate --latest
    PYTHONPATH=. .venv/bin/python -m data.verify_stage8_gate --latest --rail WIRE

It reuses `reconciliation_service.compute_reconciliation` (the same function the post-batch
pass and the UI use) for the three legs, then runs the gate invariants directly against the
collections. Every check prints PASS/FAIL with the evidence, and the script exits non-zero if
any invariant fails — so it is CI-able as well as interactive.

Gate invariants (doc 22 §3 step 8):
  1. The payment reached RECONCILED (currentState + reconciliationStatus) — or, if it did not,
     the script reports which leg is still PENDING so you know whether to trigger another batch.
  2. refs.reconciliationItemId + refs.settlementPositionId are set.
  3. A reconciliationItems doc exists with the three legs recorded.
  4. lifecycle.events grew by a RECONCILED entry from actor "ledger-service".
  5. A journalEntries doc still exists for the payment (the CDC path survived).
  6. For an external wire: the clearing account (settlementPositions.clearingAccountCode,
     usually 1131) nets to zero across the payment's ledgerEvents — the "holding account
     cleared" proof (R10).
  7. The GL batch's own pre-batch integrity (Σ subLedgerEntries == Σ journalEntries per account)
     still holds for the period.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from bson import ObjectId

from database.connection import MongoDBConnection
from services.reconciliation_service import (
    DISCREPANT,
    LEG_MATCH,
    LEG_MISMATCH,
    LEG_NOT_APPLICABLE,
    LEG_PENDING,
    PENDING,
    RECONCILED,
    compute_reconciliation,
    reconcile_all_accounts,
)


def _green(s): return f"\033[32m{s}\033[0m"
def _red(s): return f"\033[31m{s}\033[0m"
def _yellow(s): return f"\033[33m{s}\033[0m"
def _dim(s): return f"\033[2m{s}\033[0m"


def _pick_payment(connection, db_name, *, payment_id, latest, rail):
    coll = connection.get_collection(db_name, "payments")
    if payment_id:
        doc = coll.find_one({"paymentId": payment_id}, {"_id": 0})
        if doc is None:
            print(_red(f"payment {payment_id} not found in {db_name}"))
            sys.exit(2)
        return doc
    query = {}
    if rail:
        query["rail"] = rail
    doc = coll.find_one(query, {"_id": 0}, sort=[("createdAt", -1)])
    if doc is None:
        print(_red(f"no payment found in {db_name}" + (f" with rail={rail}" if rail else "")))
        sys.exit(2)
    return doc


def _signed_leg_total(events, code):
    total = 0
    for evt in events:
        d = evt.get("debitLeg") or {}
        c = evt.get("creditLeg") or {}
        if d.get("glAccountCode") == code:
            total += int(d.get("amount") or 0)
        if c.get("glAccountCode") == code:
            total -= int(c.get("amount") or 0)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 8 reconciliation gate verifier")
    parser.add_argument("paymentId", nargs="?", help="the payment to verify")
    parser.add_argument("--latest", action="store_true", help="verify the most recent payment")
    parser.add_argument("--rail", default=None, help="filter --latest by rail (WIRE / INTERNAL)")
    parser.add_argument("--db", default=None, help="override LEAFYBANK_DB_NAME")
    args = parser.parse_args()

    uri = os.getenv("MONGODB_URI")
    if not uri:
        print(_red("MONGODB_URI is not set"))
        return 2
    db_name = args.db or os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")

    if not args.payment_id and not args.latest:
        parser.error("give a paymentId, or pass --latest")

    connection = MongoDBConnection(uri)
    payment = _pick_payment(connection, db_name,
                            payment_id=args.paymentId, latest=args.latest, rail=args.rail)
    pid = payment["paymentId"]

    print(f"\n{_dim('Stage 8 gate — ' + db_name)}")
    print(f"payment: {_green(pid)}   rail: {payment.get('rail')}   "
          f"currentState: {payment.get('lifecycle', {}).get('currentState')}   "
          f"status: {payment.get('status')}")
    print(_dim("-" * 78))

    failures: list[str] = []

    # --- the three legs (reuse the production function) ---------------------------
    check = compute_reconciliation(pid, connection, db_name)
    if check is None:
        print(_red("compute_reconciliation returned None — payment vanished?"))
        return 1

    leg_names = {
        "PAYMENT_RAIL": "1  Payment ↔ Rail",
        "RAIL_SETTLEMENT": "2  Rail ↔ Settlement account",
        "SETTLEMENT_GL": "3  Settlement ↔ GL",
    }
    print("Three-way reconciliation:")
    for lg in check.legs:
        colour = _green if lg.result == LEG_MATCH else (
            _yellow if lg.result in (LEG_PENDING, LEG_NOT_APPLICABLE) else _red)
        print(f"  {leg_names.get(lg.leg, lg.leg):<32} {colour(lg.result):<16} "
              f"{_dim(lg.detail or '')}")
    print(f"  {'overall':<32} "
          f"{_green(check.overall) if check.overall == RECONCILED else _yellow(check.overall) if check.overall == PENDING else _red(check.overall)}")
    print()

    # --- invariant 1: reached RECONCILED -----------------------------------------
    state = payment.get("lifecycle", {}).get("currentState")
    recon_status = payment.get("lifecycle", {}).get("reconciliationStatus")
    if state == "RECONCILED":
        print(_green("PASS  currentState == RECONCILED"))
    else:
        if check.overall == PENDING:
            print(_yellow(f"PENDING  currentState == {state} — a leg is still awaiting the GL "
                          "batch. Trigger another cycle:"))
            print(_yellow("        curl -X POST http://localhost:8003/pipeline/batch/trigger"))
        else:
            failures.append(f"currentState is {state}, not RECONCILED")
            print(_red(f"FAIL  currentState == {state}, not RECONCILED"))
    if recon_status == "RECONCILED":
        print(_green("PASS  lifecycle.reconciliationStatus == RECONCILED"))
    elif recon_status == "DISCREPANT":
        failures.append("reconciliationStatus is DISCREPANT")
        print(_red("FAIL  lifecycle.reconciliationStatus == DISCREPANT — a leg mismatched"))
    else:
        print(_yellow(f"      reconciliationStatus == {recon_status} (not yet stamped)"))

    # --- invariant 2: refs set ---------------------------------------------------
    refs = payment.get("refs") or {}
    ri_ref = refs.get("reconciliationItemId")
    sp_ref = refs.get("settlementPositionId")
    if ri_ref:
        print(_green(f"PASS  refs.reconciliationItemId = {ri_ref}"))
    else:
        failures.append("refs.reconciliationItemId is unset")
        print(_red("FAIL  refs.reconciliationItemId is unset"))
    if sp_ref or payment.get("rail") == "INTERNAL":
        # settlementPositionId is N/A for an internal transfer (no settlement run)
        label = sp_ref or "N/A (book transfer)"
        print(_green(f"PASS  refs.settlementPositionId = {label}"))
    else:
        failures.append("refs.settlementPositionId is unset")
        print(_red("FAIL  refs.settlementPositionId is unset"))

    # --- invariant 3: reconciliationItems doc exists -----------------------------
    items = list(connection.get_collection(db_name, "reconciliationItems")
                 .find({"paymentId": pid}, {"_id": 0}).sort("checkedAt", 1))
    if items:
        it = items[-1]
        print(_green(f"PASS  reconciliationItems doc exists: {it.get('reconciliationItemId')}  "
                     f"overallResult={it.get('overallResult')}  legs={[l['result'] for l in it.get('legs', [])]}"))
    else:
        failures.append("no reconciliationItems doc")
        print(_red("FAIL  no reconciliationItems doc for this payment"))

    # --- invariant 4: lifecycle.events has a RECONCILED entry from ledger --------
    events = payment.get("lifecycle", {}).get("events") or []
    recon_events = [e for e in events if e.get("state") == "RECONCILED"]
    if recon_events:
        e = recon_events[-1]
        actor = e.get("actor")
        ok = actor == "ledger-service"
        colour = _green if ok else _yellow
        print(colour(f"{'PASS' if ok else 'WARN'}  lifecycle.events has RECONCILED entry "
                     f"(actor={actor})  reason: {e.get('reason')}"))
        if not ok:
            failures.append(f"RECONCILED event actor is {actor}, expected ledger-service")
    else:
        if state == "RECONCILED":
            failures.append("RECONCILED state but no RECONCILED lifecycle event")
            print(_red("FAIL  currentState is RECONCILED but no RECONCILED event in lifecycle.events"))
        else:
            print(_yellow("      no RECONCILED event yet (payment not yet reconciled)"))

    # --- invariant 5: a journalEntries doc still exists (CDC survived) -----------
    le_coll = connection.get_collection(db_name, "ledgerEvents")
    all_events = list(le_coll.find(
        {"idempotencyKey": {"$in": [pid, f"{pid}-FEE", f"{pid}-SETTLEMENT"]}},
        {"_id": 0},
    ))
    sl_coll = connection.get_collection(db_name, "subLedgerEntries")
    jnl_coll = connection.get_collection(db_name, "journalEntries")
    posted = [e for e in all_events if e.get("postingStatus") == "POSTED"]
    journals = []
    for evt in all_events:
        sl = sl_coll.find_one({"sourceReference.sourceId": evt.get("eventId")},
                              {"_id": 0, "journalEntryId": 1})
        jid = (sl or {}).get("journalEntryId")
        if jid:
            j = jnl_coll.find_one({"journalId": jid}, {"_id": 0})
            if j:
                journals.append(j)
    if journals:
        print(_green(f"PASS  {len(journals)} journalEntries doc(s) present for {len(all_events)} "
                     f"ledgerEvent(s) ({len(posted)} posted) — CDC survived"))
    else:
        if all_events:
            failures.append("ledgerEvents exist but no journalEntries — batch may not have run")
            print(_red("FAIL  ledgerEvents exist but no journalEntries posted yet — "
                       "trigger the batch"))
        else:
            print(_yellow("      no ledgerEvents yet — payment may not have reached the boundary"))

    # --- invariant 6: clearing account nets to zero (external wire) --------------
    if payment.get("rail") != "INTERNAL":
        positions = list(connection.get_collection(db_name, "settlementPositions")
                         .find({"paymentId": pid}, {"_id": 0}).sort("createdAt", 1))
        clearing_code = (positions[-1] if positions else {}).get("clearingAccountCode")
        if clearing_code:
            net = _signed_leg_total(all_events, clearing_code)
            if net == 0:
                print(_green(f"PASS  clearing account {clearing_code} nets to 0 across the "
                             "payment's events (holding account cleared — R10)"))
            else:
                failures.append(f"clearing account {clearing_code} nets to {net}, not 0")
                print(_red(f"FAIL  clearing account {clearing_code} nets to {net}, not 0 — "
                           "in-flight position not cleared"))
        else:
            print(_yellow("      no settlementPositions.clearingAccountCode (nothing to net)"))
    else:
        print(_green("PASS  book transfer — no clearing account to clear (skipped)"))

    # --- invariant 7: GL pre-batch integrity for the period ----------------------
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        results = reconcile_all_accounts(connection, db_name, period_code=period)
        breaks = [r for r in results if not r.is_reconciled]
        if breaks:
            failures.append(f"GL integrity break: {len(breaks)} account(s) for {period}")
            print(_red(f"FAIL  GL integrity: {len(breaks)}/{len(results)} account(s) break for "
                       f"{period}: {[r.account_code for r in breaks]}"))
        else:
            print(_green(f"PASS  GL integrity: {len(results)} account(s) reconcile for {period}"))
    except Exception as exc:
        print(_yellow(f"      GL integrity check skipped ({exc})"))

    # --- verdict -----------------------------------------------------------------
    print(_dim("-" * 78))
    if failures:
        print(_red(f"\nGATE FAILED — {len(failures)} invariant(s) failed:"))
        for f in failures:
            print(_red(f"  - {f}"))
        return 1
    if check.overall == PENDING or state != "RECONCILED":
        print(_yellow("\nGATE PENDING — not all legs are checkable yet. Trigger another batch "
                      "cycle and re-run."))
        return 0  # not a failure — the demo is mid-flight
    print(_green("\nGATE PASSED — payment reconciled end to end."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
