"""One-time cleanup: release `CUTOFF-DEMO-` payments stranded at a hold (plan-cutoff-release R3).

Run from ``backend/transactions``::

    python -m data.release_cutoff_stranded                      # dry run: list what it would do
    python -m data.release_cutoff_stranded --apply              # release them
    python -m data.release_cutoff_stranded --apply --payment-id PAY-077a5916 --payment-id ...

Discovery is by data, never by hard-coded id: a payment with a `CUTOFF-DEMO-` client reference
and a `demo.clockRunId`, in a non-closed status. `--payment-id` narrows the run to those
payments; one that is missing, or not a cutoff-demo payment, is reported and fails the run.

`--apply` takes each run through the same code the janitor uses: a run whose clock expired
(`demoClocks` has a 24 h TTL) is recovered first, then `release_run` acts as `cutoff-cleanup`.
Re-running is safe; payments already settling just settle (about 30 s). Exit code is 1 when a
named id is missing or ineligible, or when any payment is still open after `--apply`.

The database is shared, so staging-dev's CDC and ledger workers pick up the results.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from typing import Iterable, Optional

from dotenv import load_dotenv

from contexts.payment_orchestration.application import cutoff_release, cutoff_scenarios
from database.connection import MongoDBConnection
from services.payments_service import PaymentsService
from shared import business_clock

logger = logging.getLogger(__name__)

ACTOR = "cutoff-cleanup"
TERMINAL_NOTE = "closed"


@dataclass(frozen=True)
class Row:
    payment_id: str
    scenario: str
    status: str
    decision: Optional[str]
    run_id: Optional[str]
    run_alive: bool
    action: str
    problem: Optional[str] = None


def _is_demo(payment: dict) -> bool:
    return (str(payment.get("clientReference") or "").startswith(cutoff_scenarios.CLIENT_REF_PREFIX)
            and bool((payment.get("demo") or {}).get("clockRunId")))


def _row(service, payment: dict) -> Row:
    run_id = payment["demo"]["clockRunId"]
    alive = service.db[business_clock.CLOCKS].find_one({"_id": run_id}) is not None
    status = payment.get("status")
    plan = cutoff_release.plan_for(payment, phase=None)
    if status in cutoff_release._TERMINAL:
        action = TERMINAL_NOTE
    else:
        action = plan.action if alive else f"RECOVER_RUN+{plan.action}"
    return Row(
        payment_id=payment["paymentId"],
        scenario=str(payment["clientReference"])[len(cutoff_scenarios.CLIENT_REF_PREFIX):],
        status=status,
        decision=(payment.get("cutoff") or {}).get("decision"),
        run_id=run_id, run_alive=alive, action=action,
    )


def plan_cleanup(service, payment_ids: Iterable[str] = ()) -> list[Row]:
    """What the cleanup would do. Pure reads.

    With no ids: every cutoff-demo payment that is not closed. With ids: exactly those, in
    any status (a closed one shows as closed); a missing or non-demo id gets a `problem`.
    """
    named = list(payment_ids)
    if not named:
        found = service.payments.find({"demo.clockRunId": {"$exists": True}})
        return [_row(service, p) for p in found
                if _is_demo(p) and p.get("status") not in cutoff_release._TERMINAL]
    rows = []
    for payment_id in named:
        payment = service.payments.find_one({"paymentId": payment_id})
        if payment is None:
            rows.append(Row(payment_id, "-", "-", None, None, False, "NONE", "not found"))
        elif not _is_demo(payment):
            rows.append(Row(payment_id, "-", payment.get("status"), None, None, False, "NONE",
                            "not a CUTOFF-DEMO payment on a clock run"))
        else:
            rows.append(_row(service, payment))
    return rows


def apply_cleanup(service, rows: list[Row]) -> list[str]:
    """Release every row's run. Returns the error lines; the caller re-reads the payments."""
    errors = []
    for run_id in dict.fromkeys(r.run_id for r in rows if r.run_id and r.action != TERMINAL_NOTE):
        try:
            live_run = cutoff_release.recover_run(service, run_id)
            cutoff_release.release_run(service, live_run, actor=ACTOR)
        except (LookupError, cutoff_release.ReleaseBusy) as exc:
            errors.append(f"{run_id}: {exc}")
    return errors


def format_table(rows: list[Row]) -> str:
    header = ("PAYMENT", "SCENARIO", "STATUS", "DECISION", "CLOCK", "ACTION / PROBLEM")
    body = [(r.payment_id, r.scenario, r.status or "-", r.decision or "-",
             "alive" if r.run_alive else "expired" if r.run_id else "-",
             r.problem or r.action) for r in rows]
    widths = [max(len(line[i]) for line in [header, *body]) for i in range(len(header))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip()
                     for line in [header, *body])


def _still_open(service, rows: list[Row]) -> list[str]:
    ids = [r.payment_id for r in rows if not r.problem]
    docs = [service.payments.find_one({"paymentId": i}) for i in ids]
    return [d["paymentId"] for d in docs if d and d.get("status") not in cutoff_release._TERMINAL]


def run(service, *, apply: bool, payment_ids: Iterable[str] = ()) -> int:
    rows = plan_cleanup(service, payment_ids)
    print(format_table(rows) if rows else "No stranded CUTOFF-DEMO payments.")
    failed = any(r.problem for r in rows)
    if not apply:
        print("\nDry run. Re-run with --apply to release these.")
        return 1 if failed else 0

    for error in apply_cleanup(service, rows):
        print(f"error: {error}")
        failed = True
    print("\nAfter release:")
    released = [r.payment_id for r in rows if not r.problem]
    if released:
        print(format_table(plan_cleanup(service, released)))
    open_ids = _still_open(service, rows)
    if open_ids:
        print(f"\nStill open: {', '.join(open_ids)} (in-flight payments settle in ~30 s; re-run)")
    return 1 if failed or open_ids else 0


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="release; default is a dry run")
    parser.add_argument("--payment-id", action="append", default=[], metavar="PAY-...",
                        help="limit to this payment; repeatable")
    args = parser.parse_args(argv)

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise SystemExit("MONGODB_URI is not set. Create backend/transactions/.env (see README).")
    service = PaymentsService(
        MongoDBConnection(uri), os.getenv("LEAFYBANK_DB_NAME", "leafy_bank_bian"),
        float(os.getenv("PAYMENT_LIMIT_USD", "1000000")))
    sys.exit(run(service, apply=args.apply, payment_ids=args.payment_id))


if __name__ == "__main__":
    main()
