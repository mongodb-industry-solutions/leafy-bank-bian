"""One-shot repair: the wrong-leg inbound settlement events from the 2026-09-29 backfill window.

## What this repairs

The inbound settle-on-arrival seam was fixed in two parts — the transactions side stamps
`clearing.settlementAccountCode`, the ledger side mirrors the legs and resolves the clearing
account from `payer` for inbound. The live backfill ran BETWEEN the two: the crash-looping
worker still held the OLD code, and un-poisoning the payment (stamping the missing field)
let that old code SUCCEED instead of crash — producing, for every inbound payment settled
in that window, a settlement event with the old outbound decomposition built off the CUSTOMER
account (resolved from `txn.payee`):

    wrong:   Dr 2111 Customer Deposits / Cr 1111 Nostro    (reverses the beneficiary's credit
                                                           into the nostro — silent corruption)
    correct: Dr 1111 Nostro / Cr 1131 Wire Clearing          (FR-7.IN1, the mirror)

Every such payment then failed leg 3 (1131 nets to +amount, not zero) and queued a
RECONCILIATION_DISCREPANCY exception. The fixed worker skips them ("already exists") —
idempotency correctly protecting a poisoned event. This script removes the poison.

⚠️ The lesson (defects.md, `cdc-consumer-seam-unsatisfied` amendment): un-poisoning a CDC
worker's input while the worker still runs old code does not make it fail loudly — it makes
it SUCCEED WRONGLY. Stop/restart the worker with the fixed code BEFORE backfilling.

## How it repairs

The GL pipeline is fully derived (events <- CDC <- payments/transactions; subLedgerEntries
<- CDC <- events; journalEntries <- batch <- subLedgerEntries), so the repair deletes the
wrong artifacts and lets the pipeline regenerate them with the fixed code:

  1. delete each wrong-leg `{paymentId}-SETTLEMENT` ledgerEvent;
  2. delete its subLedgerEntries rows;
  3. for each journal those rows fed: reset its REMAINING rows to un-journaled and delete
     the journal — per-account journaled-vs-journal sums stay equal on both sides, so the
     pre-batch gate stays green, and the next batch re-aggregates those rows deterministically;
  4. touch each repaired payment (`updatedAt`) — the update event re-triggers the (fixed)
     settlement worker, which rebuilds the event with the mirror legs;
  5. DISMISS the stale OPEN RECONCILIATION_DISCREPANCY exceptions — they were artifacts of
     the bug window, not real discrepancies.

Then: the projection worker derives the subledger rows, the next GL batch (or
`curl -X POST :8003/pipeline/batch/trigger`) journals them and re-stamps `refs.journalEntryId`,
and the post-batch sweep stamps RECONCILED.

## Run

    cd backend/ledger
    .venv/bin/python data/repair_inbound_settlement_events.py             # dry run — plan only
    .venv/bin/python data/repair_inbound_settlement_events.py --status    # what is stored NOW
    .venv/bin/python data/repair_inbound_settlement_events.py --apply     # execute

The ledger service must be running the FIXED code (restarted after 2026-09-29) before
`--apply` — otherwise step 4 would re-trigger the old worker and reproduce the poison.

`--status` is the diagnostic: for every inbound payment it prints the CURRENT settlement
event's legs + postingStatus + the 1131 net. That discriminates the three possible states
after a repair round — rebuilt-mirror-and-journaled (leg 3 should MATCH at the next sweep),
rebuilt-mirror-but-PENDING (just needs another batch trigger), or a WRONG event still
present (the delete failed or the rebuild used stale code — re-run --apply).
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Running a file by path puts THIS directory (data/) on sys.path, not the service root —
# so the ledger's own `database/` package would not be importable. Put the root on the
# path explicitly (the same thing `python -m` would do).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The correct inbound settlement event is the mirror (FR-7.IN1): debit the nostro, credit
# the clearing account. The wrong-window event credits the NOSTRO (1111) instead — that is
# the discriminator: an inbound settlement event whose CREDIT leg is not the clearing
# account came from the old code.
_CLEARING_CODE = "1131"
_SETTLEMENT_SUFFIX = "-SETTLEMENT"
_EXCEPTION_CATEGORY = "RECONCILIATION_DISCREPANCY"

_NOTE = (
    "Repair 2026-09-29: this exception was an artifact of the wrong-leg inbound settlement "
    "events created by the pre-restart worker during the backfill window (defects.md, "
    "cdc-consumer-seam-unsatisfied). The events were deleted and regenerated with the "
    "mirror legs; the discrepancy is not real."
)


def _status(payments, events) -> None:
    """Diagnostic: for every inbound payment, what settlement event is stored NOW.

    One line per payment with the event's legs + postingStatus, and the 1131 net across
    the payment's principal + settlement events — the number leg 3 compares against zero.
    Discriminates the three post-repair states (see the module docstring).
    """
    from services.reconciliation_service import _clearing_account_net  # noqa: import for reuse

    for p in payments.find({"direction": "INBOUND"}, {"paymentId": 1}):
        payment_id = p["paymentId"]
        all_events = list(events.find(
            {"idempotencyKey": {"$in": [
                payment_id, f"{payment_id}-FEE", f"{payment_id}-SETTLEMENT",
            ]}},
            {"_id": 0},
        ))
        settlement = next(
            (e for e in all_events
             if e.get("idempotencyKey") == f"{payment_id}{_SETTLEMENT_SUFFIX}"), None
        )
        principal = next(
            (e for e in all_events if e.get("idempotencyKey") == payment_id), None
        )
        if settlement is None:
            legs = "NO EVENT (awaiting the worker)"
        else:
            created = settlement.get("createdAt")
            # `createdAt` is the discriminator the legs alone can't give: the ORIGINALS
            # from the backfill window carry a timestamp from that window; a rebuild
            # carries one from the repair's touch. Same wrong legs, opposite causes —
            # originals mean the delete matched nothing; rebuilds mean whatever is running
            # re-inserted them with old code.
            # The WRITER's own signature, and a better discriminator than `createdAt`:
            # the fixed worker words an inbound event "Inbound settlement posting", the old
            # (outbound-only) code words it "External settlement posting". An inbound event
            # that says "External" was written by pre-fix code — whatever its timestamp, and
            # regardless of which image the host thinks is running. `createdAt` only says
            # WHEN; this says WHICH CODE.
            desc = settlement.get("description") or ""
            writer = "OLD-CODE" if desc.startswith("External") else "fixed"
            legs = (
                f"Dr {(settlement.get('debitLeg') or {}).get('glAccountCode')} / "
                f"Cr {(settlement.get('creditLeg') or {}).get('glAccountCode')} "
                f"[{settlement.get('postingStatus')}] "
                f"writer={writer} created={created}"
            )
        net = _clearing_account_net(all_events, _CLEARING_CODE) if all_events else 0
        flag = "OK" if net == 0 else "NET!=0"
        print(f"{payment_id}: {legs} · principal={'yes' if principal else 'NO'} "
              f"· 1131 net={net} [{flag}]")


def main() -> None:
    apply = "--apply" in sys.argv
    status = "--status" in sys.argv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    from database.connection import MongoDBConnection

    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise SystemExit("MONGODB_URI is not set")
    db = MongoDBConnection(uri).get_database(
        os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian")
    )
    payments, events = db["payments"], db["ledgerEvents"]
    subledger, journals = db["subLedgerEntries"], db["journalEntries"]
    exceptions = db["exceptions"]

    if status:
        _status(payments, events)
        return

    inbound = list(payments.find({"direction": "INBOUND"}, {"paymentId": 1}))
    now = datetime.now(timezone.utc)

    plan = []
    for p in inbound:
        payment_id = p["paymentId"]
        event = events.find_one({"idempotencyKey": f"{payment_id}{_SETTLEMENT_SUFFIX}"})
        if event is None:
            continue
        credit_code = (event.get("creditLeg") or {}).get("glAccountCode")
        if credit_code == _CLEARING_CODE:
            continue  # already the mirror — nothing to repair
        rows = list(subledger.find({"sourceReference.sourceId": event["eventId"]}))
        journal_ids = sorted({r["journalEntryId"] for r in rows if r.get("journalEntryId")})
        plan.append({
            "paymentId": payment_id,
            "eventId": event["eventId"],
            "wrongLegs": (
                f"Dr {(event.get('debitLeg') or {}).get('glAccountCode')} / "
                f"Cr {credit_code}"
            ),
            "amount": (event.get("debitLeg") or {}).get("amount"),
            "subledgerRows": len(rows),
            "journals": journal_ids,
        })

    if not plan:
        print("Nothing to repair — every inbound settlement event already carries the "
              "mirror legs (Dr nostro / Cr clearing).")
        return

    print(f"{len(plan)} wrong-leg inbound settlement event(s) found:")
    for entry in plan:
        print(f"  {entry['paymentId']}: {entry['wrongLegs']} "
              f"amount={entry['amount']} rows={entry['subledgerRows']} "
              f"journals={entry['journals']}")

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to repair.")
        return

    seen_journals: set[str] = set()
    counts = {"events": 0, "rows": 0, "journals": 0, "rows_reset": 0,
              "payments_touched": 0, "exceptions_dismissed": 0}
    for entry in plan:
        # 1+2: the wrong event and its derived rows. Both deletes are VERIFIED — a delete
        # that silently matched nothing leaves the wrong event in place, and the worker's
        # rebuild then collides with it on the unique key and skips ("already exists"),
        # leaving the poison in. Found live 2026-09-29: the first --apply round reported
        # success unconditionally; the sweep immediately re-flagged every payment.
        counts["rows"] += subledger.delete_many(
            {"sourceReference.sourceId": entry["eventId"]}
        ).deleted_count
        result = events.delete_one({"eventId": entry["eventId"]})
        if result.deleted_count != 1:
            print(f"  ⚠️ {entry['paymentId']}: settlement event NOT deleted "
                  f"(matched {result.deleted_count}) — it may have been rebuilt between "
                  "the scan and this delete. Re-run --status, then --apply.")
            continue
        counts["events"] += 1

        # 3: each journal the wrong rows fed — reset its remaining rows to un-journaled and
        # delete it, so the next batch re-aggregates them (and per-account
        # journaled-vs-journal sums stay equal on both sides of the deletion).
        for journal_id in entry["journals"]:
            if journal_id in seen_journals:
                continue
            seen_journals.add(journal_id)
            result = subledger.update_many(
                {"journalEntryId": journal_id}, {"$set": {"journalEntryId": ""}}
            )
            counts["rows_reset"] += result.modified_count
            if journals.delete_one({"journalId": journal_id}).deleted_count:
                counts["journals"] += 1

        # 4: touch the payment so the FIXED worker rebuilds the event (change event ->
        # fullDocument.lifecycle.settlementStatus == SETTLED matches its stream).
        payments.update_one(
            {"paymentId": entry["paymentId"]}, {"$set": {"updatedAt": now}}
        )
        counts["payments_touched"] += 1

        # 5: dismiss the stale exception (artifact, not a real discrepancy).
        result = exceptions.update_many(
            {"paymentId": entry["paymentId"], "category": _EXCEPTION_CATEGORY,
             "status": "OPEN"},
            {"$set": {
                "status": "DISMISSED",
                "resolution": {"action": "DISMISS", "by": "ledger-repair",
                               "at": now, "note": _NOTE},
                "updatedAt": now,
            }},
        )
        counts["exceptions_dismissed"] += result.modified_count

    print("\nRepaired:", counts)

    # VERIFY THE REBUILD. Round 1 (2026-09-29) printed a success summary and stopped, and
    # the wrong events were still there afterwards — a summary that was true about its own
    # writes and wrong about the outcome. Step 4 hands the real work to an asynchronous
    # worker, so the only honest completion check is to read back what that worker produced.
    print("\nWaiting 20s for the settlement worker to rebuild the events, then verifying…")
    time.sleep(20)
    stale = []
    for entry in plan:
        rebuilt = events.find_one(
            {"idempotencyKey": f"{entry['paymentId']}{_SETTLEMENT_SUFFIX}"}
        )
        if rebuilt is None:
            stale.append((entry["paymentId"], "NOT REBUILT (worker not running?)"))
            continue
        credit = (rebuilt.get("creditLeg") or {}).get("glAccountCode")
        if credit != _CLEARING_CODE:
            stale.append((entry["paymentId"], f"REBUILT WRONG (Cr {credit}) — OLD CODE"))
    if stale:
        print("\n⛔ THE REBUILD IS NOT CLEAN — do NOT re-run --apply yet:")
        for payment_id, why in stale:
            print(f"  {payment_id}: {why}")
        print(
            "\nEither the ledger is not running, or it is running pre-fix code.\n"
            "  `writer=OLD-CODE`  -> the ledger process predates the worker fix. Under\n"
            "                        `make dev`, uvicorn --reload only reloads on a file\n"
            "                        change it observed while running: a process started\n"
            "                        BEFORE the edit reloads, one started after does not\n"
            "                        need to — but a process whose reload never fired (or\n"
            "                        which was started from a stale interpreter) keeps the\n"
            "                        old module. Restart `make dev-ledger` and re-check.\n"
            "  `NOT REBUILT`      -> the settlement_worker thread is not watching. Check\n"
            "                        ENABLE_CHANGE_STREAMS is not false, and that the\n"
            "                        ledger log shows 'settlement_worker starting'.\n"
            "Confirm with `--status` that new events read writer=fixed, then re-run --apply."
        )
        return

    print("\n✅ Every repaired event rebuilt with the mirror legs (Cr 1131).")
    print("\nNext: the settlement worker rebuilds each event (seconds), the projection "
          "worker derives the rows, then trigger the batch to journal + re-stamp:\n"
          "  curl -X POST http://localhost:8003/pipeline/batch/trigger\n"
          "The post-batch sweep then stamps each payment RECONCILED.")


if __name__ == "__main__":
    main()
