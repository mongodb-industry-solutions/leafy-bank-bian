"""Read-only evidence for the Reconciliation Agent: statement candidates, correspondent
profile, resolution precedents. Direct pymongo reads on the shared DB, like `payment_trace`.

Candidate scoring is deterministic and lives in `rank_candidates`, a pure function, so the
agent can only ever pick from a list the code produced (policy.check_proposal enforces it).
No Atlas Search here — fuzzy reference repair is Wei You's demo's lane (`_state.md`
no-overlap rule); this is a key comparison over a handful of unmatched lines.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Optional

import policy

logger = logging.getLogger(__name__)

STATEMENT_PURPOSE = "ACCOUNT_STATEMENT"
DEFAULT_CHARGE = 25.0  # camt053.CORRESPONDENT_CHARGE; used when the correspondent has no policy
MAX_CANDIDATES = 5
_DATE_SLACK = timedelta(days=1)

SCORE_EXACT = 1.0
SCORE_PREFIX = 0.8      # TRUNC16+SUFFIX: the id's core, upper-cased, cut to 16 with a suffix
SCORE_TOKEN = 0.5
SCORE_AMOUNT_ONLY = 0.2


def reference_score(reference: Optional[str], payment_id: Optional[str]) -> tuple[float, str]:
    """How strongly a statement reference points at a payment id, with the basis."""
    ref, pid = (reference or "").upper(), (payment_id or "").upper()
    if not ref or not pid:
        return SCORE_AMOUNT_ONLY, "amount only"
    if ref == pid:
        return SCORE_EXACT, "exact reference"
    core = pid.split("-", 1)[-1]
    if core and (ref.startswith(core) or core.startswith(ref.split("/", 1)[0])):
        return SCORE_PREFIX, "re-keyed reference (id core + correspondent suffix)"
    tokens = lambda s: {t for t in re.split(r"[^A-Z0-9]+", s) if len(t) >= 4}
    if tokens(ref) & tokens(pid):
        return SCORE_TOKEN, "shared reference token"
    return SCORE_AMOUNT_ONLY, "amount only"


def rank_candidates(rows: list[dict]) -> list[dict]:
    """Score each `{reference, paymentId, amount, expectedAmount, ...}` row; best first."""
    ranked = []
    for r in rows:
        score, basis = reference_score(r.get("reference"), r.get("paymentId"))
        delta = round(float(r.get("amount") or 0) - float(r.get("expectedAmount") or 0), 2)
        ranked.append({**r, "score": score, "basis": basis, "amountDelta": delta})
    ranked.sort(key=lambda c: (-c["score"], abs(c["amountDelta"])))
    return ranked[:MAX_CANDIDATES]


def _charge_for(db: Any, bic: Optional[str]) -> float:
    profile = _correspondent(db, bic) or {}
    policy_doc = profile.get("chargePolicy") or {}
    charges = [v for k, v in policy_doc.items() if k != "currency" and isinstance(v, (int, float))]
    return max(charges) if charges else DEFAULT_CHARGE


def _correspondent(db: Any, bic: Optional[str]) -> Optional[dict]:
    if not bic:
        return None
    return db["correspondentBanks"].find_one(
        {"recordType": "BIC_DIRECTORY", "$or": [{"identification.value": bic}, {"swiftCode": bic}]},
        {"_id": 0, "bankName": 1, "country": 1, "chargePolicy": 1, "referenceFormat": 1},
    )


def _latest_position(db: Any, payment_id: str) -> Optional[dict]:
    rows = list(db["settlementPositions"].find({"paymentId": payment_id}, {"_id": 0})
                .sort("createdAt", -1).limit(1))
    return rows[0] if rows else None


def find_candidates(db: Any, exc: dict) -> list[dict]:
    """Ranked pairings for a MISSING payment (lines) or an ORPHANED line (payments)."""
    category = exc.get("category")
    if category == policy.CATEGORY_MISSING:
        return rank_candidates(_lines_for_payment(db, exc["paymentId"]))
    if category == policy.CATEGORY_ORPHANED:
        return rank_candidates(_payments_for_line(db, exc))
    return []


def _lines_for_payment(db: Any, payment_id: str) -> list[dict]:
    position = _latest_position(db, payment_id)
    if position is None:
        return []
    payment = db["payments"].find_one({"paymentId": payment_id},
                                      {"_id": 0, "correspondent": 1}) or {}
    bic = (payment.get("correspondent") or {}).get("correspondentBic")
    expected = float(position.get("expectedAmount") or position.get("grossAmount") or 0)
    tolerance = _charge_for(db, bic) + 0.005
    stmt_match: dict = {"purpose": STATEMENT_PURPOSE,
                        "statement.accountCode": position.get("settlementAccountCode"),
                        "statement.historical": {"$ne": True},
                        "entries.recon.status": "UNMATCHED"}
    settled_at = (position.get("expectedWindow") or {}).get("from")
    if settled_at is not None:
        stmt_match["statement.window.to"] = {"$gte": settled_at - _DATE_SLACK}
        stmt_match["statement.window.from"] = {"$lte": settled_at + _DATE_SLACK}
    pipeline = [
        {"$match": stmt_match},
        {"$unwind": "$entries"},
        {"$match": {"entries.recon.status": "UNMATCHED",
                    "entries.amount": {"$gte": expected - tolerance, "$lte": expected + tolerance}}},
        {"$project": {"_id": 0, "paymentMessageId": 1, "lineNo": "$entries.lineNo",
                      "reference": "$entries.reference", "amount": "$entries.amount",
                      "charges": "$entries.charges", "bookingDate": "$statement.window.to"}},
    ]
    rows = list(db["paymentMessages"].aggregate(pipeline))
    return [{**r, "paymentId": payment_id, "expectedAmount": expected} for r in rows]


def _payments_for_line(db: Any, exc: dict) -> list[dict]:
    subject = exc.get("subjectRef") or {}
    stmt = db["paymentMessages"].find_one(
        {"paymentMessageId": subject.get("paymentMessageId"), "purpose": STATEMENT_PURPOSE},
        {"_id": 0, "statement": 1, "entries": 1})
    if stmt is None:
        return []
    line = next((e for e in stmt.get("entries") or [] if e.get("lineNo") == subject.get("lineNo")), None)
    if line is None:
        return []
    amount = float(line.get("amount") or 0)
    tolerance = _charge_for(db, line.get("counterpartyBic")) + 0.005
    pipeline = [
        {"$match": {"settlementAccountCode": (stmt.get("statement") or {}).get("accountCode"),
                    "actualAmount": None,
                    "expectedAmount": {"$gte": amount - tolerance, "$lte": amount + tolerance}}},
        {"$lookup": {"from": "payments", "localField": "paymentId", "foreignField": "paymentId",
                     "as": "payment",
                     "pipeline": [{"$project": {"_id": 0, "lifecycle": 1}}]}},
        {"$match": {"payment.lifecycle.currentState": {"$in": ["SETTLED", "POSTED"]},
                    "payment.lifecycle.reconciliationStatus": {"$ne": "RECONCILED"}}},
        {"$project": {"_id": 0, "paymentId": 1, "expectedAmount": 1}},
        {"$limit": 50},
    ]
    rows = list(db["settlementPositions"].aggregate(pipeline))
    return [{**r, "paymentMessageId": subject.get("paymentMessageId"),
             "lineNo": subject.get("lineNo"), "reference": line.get("reference"),
             "amount": amount} for r in rows]


def _percentile(sorted_values: list[float], p: float) -> Optional[float]:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, max(0, round(p * (len(sorted_values) - 1))))
    return sorted_values[idx]


def correspondent_profile(db: Any, bic: str) -> dict:
    """Charge policy, reference format, and booking lag (statement booked − submitted)."""
    profile = _correspondent(db, bic)
    pipeline = [
        {"$match": {"actualBookedAt": {"$ne": None}}},
        {"$lookup": {"from": "payments", "localField": "paymentId", "foreignField": "paymentId",
                     "as": "payment",
                     "pipeline": [{"$project": {"_id": 0, "correspondent": 1, "clearing": 1}}]}},
        {"$unwind": "$payment"},
        {"$match": {"payment.correspondent.correspondentBic": bic,
                    "payment.clearing.submittedAt": {"$ne": None}}},
        {"$sort": {"actualBookedAt": -1}},
        {"$limit": 200},
        {"$project": {"_id": 0, "lagSeconds": {"$divide": [
            {"$subtract": ["$actualBookedAt", "$payment.clearing.submittedAt"]}, 1000]}}},
    ]
    try:
        lags = sorted(r["lagSeconds"] for r in db["settlementPositions"].aggregate(pipeline)
                      if isinstance(r.get("lagSeconds"), (int, float)))
    except Exception:  # noqa: BLE001 — lag is evidence, not a precondition.
        logger.warning("correspondent lag read failed for %s", bic, exc_info=True)
        lags = []
    return {
        "bic": bic,
        "known": profile is not None,
        "bankName": (profile or {}).get("bankName"),
        "chargePolicy": (profile or {}).get("chargePolicy"),
        "referenceFormat": (profile or {}).get("referenceFormat"),
        "bookingLag": {"samples": len(lags), "p50Seconds": _percentile(lags, 0.5),
                       "p90Seconds": _percentile(lags, 0.9)},
    }


def resolution_precedents(db: Any, bic: str, category: str, limit: int = 10) -> list[dict]:
    """The last resolved exceptions of this category for this correspondent, with actions.

    Backfilled precedents carry the BIC on `historical`; live ones are found through their
    payment's correspondent.
    """
    live_ids = [p["paymentId"] for p in db["payments"].find(
        {"correspondent.correspondentBic": bic}, {"_id": 0, "paymentId": 1}).limit(500)]
    rows = db["exceptions"].find(
        {"category": category, "status": {"$in": ["RESOLVED", "DISMISSED"]},
         "$or": [{"historical.correspondentBic": bic}, {"paymentId": {"$in": live_ids}}]},
        {"_id": 0, "exceptionId": 1, "resolution": 1, "historical": 1, "detail": 1, "createdAt": 1},
    ).sort("createdAt", -1).limit(limit)
    return [{
        "exceptionId": r.get("exceptionId"),
        "action": (r.get("resolution") or {}).get("action"),
        "note": (r.get("resolution") or {}).get("note"),
        "chargeBearer": (r.get("historical") or {}).get("chargeBearer"),
        "statementOutcome": (r.get("historical") or {}).get("statementOutcome"),
        "discrepancyAmount": (r.get("detail") or {}).get("discrepancyAmount"),
        "at": r.get("createdAt"),
    } for r in rows]
