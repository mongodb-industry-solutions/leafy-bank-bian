"""Read-only evidence for the Reconciliation Agent: statement candidates, correspondent
profile, resolution precedents. Direct pymongo reads on the shared DB, like `payment_trace`.

Candidate scoring is deterministic and lives in `rank_candidates`, a pure function, so the
agent can only ever pick from a list the code produced (policy.check_proposal enforces it).
The reference score always decides; a Voyage rerank of the line's narrative (`AddtlNtryInf`)
against the payment's remittance and beneficiary only orders candidates that tie on it.
No Atlas Search fuzzy reference repair here — that is Wei You's demo's lane (`_state.md`).
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Callable, Optional

import policy

logger = logging.getLogger(__name__)

# (query, documents) -> one relevance per document, or None when unavailable.
Reranker = Callable[[str, list[str]], Optional[list[float]]]

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


def rank_candidates(rows: list[dict], semantic: Optional[list[float]] = None) -> list[dict]:
    """Score each `{reference, paymentId, amount, expectedAmount, ...}` row; best first.

    `semantic` (one relevance per row) breaks ties on the reference score before the amount
    delta does; it never lifts a weaker reference match over a stronger one.
    """
    ranked = []
    for i, r in enumerate(rows):
        score, basis = reference_score(r.get("reference"), r.get("paymentId"))
        delta = round(float(r.get("amount") or 0) - float(r.get("expectedAmount") or 0), 2)
        ranked.append({**r, "score": score, "basis": basis, "amountDelta": delta,
                       "semanticScore": semantic[i] if semantic else None})
    ranked.sort(key=lambda c: (-c["score"], -(c["semanticScore"] or 0), abs(c["amountDelta"])))
    return ranked[:MAX_CANDIDATES]


def _rerank_rows(rows: list[dict], query: Optional[str], doc_key: str,
                 rerank: Optional[Reranker]) -> Optional[list[float]]:
    if rerank is None or not query or not rows:
        return None
    docs = [r.get(doc_key) or "" for r in rows]
    if not any(docs):
        return None
    return rerank(query, docs)


def known_charges(db: Any, bic: Optional[str]) -> list[float]:
    """Every charge this correspondent levies; the default charge when it has none.

    A FEE cause is admissible only at one of these amounts — a delta that matches
    none is an AMOUNT_MISMATCH, whatever the model calls it (policy.fee_cause_refusal).
    """
    profile = _correspondent(db, bic) or {}
    policy_doc = profile.get("chargePolicy") or {}
    charges = [v for k, v in policy_doc.items() if k != "currency" and isinstance(v, (int, float))]
    return sorted({round(float(c), 2) for c in charges}) or [DEFAULT_CHARGE]


def _charge_for(db: Any, bic: Optional[str]) -> float:
    return max(known_charges(db, bic))


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


def payment_narrative(payment: dict) -> Optional[str]:
    """What we sent: remittance text and beneficiary — the counterpart of a line's narrative."""
    remittance = payment.get("remittance") or {}
    parts = [remittance.get("unstructured") or remittance.get("reference")
             or remittance.get("invoiceNo"), (payment.get("creditor") or {}).get("name")]
    return " ".join(p for p in parts if p) or None


def find_candidates(db: Any, exc: dict, rerank: Optional[Reranker] = None) -> list[dict]:
    """Ranked pairings for a MISSING payment (lines) or an ORPHANED line (payments)."""
    category = exc.get("category")
    if category == policy.CATEGORY_MISSING:
        rows, query = _lines_for_payment(db, exc["paymentId"])
        return rank_candidates(rows, _rerank_rows(rows, query, "narrative", rerank))
    if category == policy.CATEGORY_ORPHANED:
        rows, query = _payments_for_line(db, exc)
        return rank_candidates(rows, _rerank_rows(rows, query, "paymentNarrative", rerank))
    return []


def _lines_for_payment(db: Any, payment_id: str) -> tuple[list[dict], Optional[str]]:
    position = _latest_position(db, payment_id)
    if position is None:
        return [], None
    payment = db["payments"].find_one({"paymentId": payment_id},
                                      {"_id": 0, "correspondent": 1, "remittance": 1,
                                       "creditor.name": 1}) or {}
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
                      "narrative": "$entries.additionalInfo",
                      "charges": "$entries.charges", "bookingDate": "$statement.window.to"}},
    ]
    rows = list(db["canonicalJsonStorage"].aggregate(pipeline))
    return ([{**r, "paymentId": payment_id, "expectedAmount": expected} for r in rows],
            payment_narrative(payment))


def _payments_for_line(db: Any, exc: dict) -> tuple[list[dict], Optional[str]]:
    subject = exc.get("subjectRef") or {}
    stmt = db["canonicalJsonStorage"].find_one(
        {"paymentMessageId": subject.get("paymentMessageId"), "purpose": STATEMENT_PURPOSE},
        {"_id": 0, "statement": 1, "entries": 1})
    if stmt is None:
        return [], None
    line = next((e for e in stmt.get("entries") or [] if e.get("lineNo") == subject.get("lineNo")), None)
    if line is None:
        return [], None
    amount = float(line.get("amount") or 0)
    tolerance = _charge_for(db, line.get("counterpartyBic")) + 0.005
    pipeline = [
        {"$match": {"settlementAccountCode": (stmt.get("statement") or {}).get("accountCode"),
                    "actualAmount": None,
                    "expectedAmount": {"$gte": amount - tolerance, "$lte": amount + tolerance}}},
        {"$lookup": {"from": "payments", "localField": "paymentId", "foreignField": "paymentId",
                     "as": "payment",
                     "pipeline": [{"$project": {"_id": 0, "lifecycle": 1, "remittance": 1,
                                                "creditor.name": 1}}]}},
        {"$match": {"payment.lifecycle.currentState": {"$in": ["SETTLED", "POSTED"]},
                    "payment.lifecycle.reconciliationStatus": {"$ne": "RECONCILED"}}},
        {"$project": {"_id": 0, "paymentId": 1, "expectedAmount": 1,
                      "remittance": {"$first": "$payment.remittance"},
                      "creditor": {"$first": "$payment.creditor"}}},
        {"$limit": 50},
    ]
    rows = list(db["settlementPositions"].aggregate(pipeline))
    return ([{"paymentId": r["paymentId"], "expectedAmount": r.get("expectedAmount"),
              "paymentNarrative": payment_narrative(r),
              "paymentMessageId": subject.get("paymentMessageId"),
              "lineNo": subject.get("lineNo"), "reference": line.get("reference"),
              "narrative": line.get("additionalInfo"), "amount": amount} for r in rows],
            line.get("additionalInfo"))


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


# ── Discrepancy checks (Doina, 2026-11-10) ───────────────────────────────────────────
#
# Before the agent calls an amount gap unexplained it rules out the ordinary explanations:
# currency, the amount fields, FX, the charge bearer and charges reported on the entry. A gap
# larger than a correspondent's fee is NOT by itself evidence of an error; the escalation
# threshold (policy.ESCALATION_THRESHOLD) is our rule for when to ask, not proof of a cause.

def _num(value) -> Optional[float]:
    return None if value is None else round(float(value), 2)


def statement_entry(db: Any, payment_id: str, position: Optional[dict]) -> Optional[dict]:
    """The statement entry the position's actual amount was booked from."""
    ref = (position or {}).get("sourceMessageRef")
    if not ref:
        return None
    stmt = db["canonicalJsonStorage"].find_one(
        {"paymentMessageId": ref, "purpose": STATEMENT_PURPOSE}, {"_id": 0, "entries": 1})
    return next((e for e in (stmt or {}).get("entries") or []
                 if (e.get("recon") or {}).get("matchedPaymentId") == payment_id
                 and not e.get("reversal")), None)


def discrepancy_checks(payment: dict, position: dict, entry: Optional[dict],
                       levied_charges: list[float],
                       threshold: float = policy.ESCALATION_THRESHOLD) -> dict:
    """Which ordinary causes could explain `expected − booked`. Pure.

    Each check reports what it found and whether that explains the gap. `unexplained` is
    true only when none does; `escalation.required` applies our internal threshold to an
    unexplained gap.
    """
    expected = _num(position.get("expectedAmount", position.get("grossAmount")))
    booked = _num(position.get("actualAmount"))
    gap = None if expected is None or booked is None else round(expected - booked, 2)
    cur_payment = payment.get("currency")
    cur_instructed = payment.get("instructedCurrency") or cur_payment
    cur_expected = position.get("expectedCurrency") or position.get("currency")
    cur_booked = (entry or {}).get("currency") or position.get("actualCurrency")
    instructed = _num(payment.get("instructedAmount"))
    amount = _num(payment.get("amount"))
    fx_rate = payment.get("fxRate") or (payment.get("fx") or {}).get("rate")
    reported = _num((entry or {}).get("charges"))
    bearer = payment.get("chargeBearer")

    currencies = {c for c in (cur_payment, cur_expected, cur_booked) if c}
    checks = [
        {"check": "CURRENCY",
         "finding": f"payment {cur_payment}, expected {cur_expected}, booked {cur_booked}",
         "explainsGap": len(currencies) > 1},
        {"check": "AMOUNT_FIELDS",
         "finding": (f"instructed {instructed}, payment amount {amount}, expected settlement "
                     f"{expected}, booked {booked}"),
         "explainsGap": None not in (instructed, amount, expected)
                        and not instructed == amount == expected},
        {"check": "FX",
         "finding": (f"FX applied at rate {fx_rate} ({cur_instructed}->{cur_payment})"
                     if fx_rate or cur_instructed != cur_payment
                     else f"no FX: instructed and settled in {cur_payment}"),
         "explainsGap": bool(fx_rate) or cur_instructed != cur_payment},
        {"check": "CHARGE_BEARER",
         "finding": (f"{bearer}: " + ("we bear all charges, so none may be deducted from the "
                                      "amount; a charge claimed from us shows as an over-debit"
                                      if bearer == "DEBT" else
                                      "a deducted charge is possible, but only a reported or "
                                      "levied charge of exactly the gap would explain it")),
         "explainsGap": False},
        {"check": "REPORTED_CHARGES",
         "finding": (f"statement entry reports charges of {reported}" if reported
                     else "no charges reported on the statement entry"),
         "explainsGap": reported is not None and gap is not None and reported == abs(gap)},
        {"check": "LEVIED_CHARGES",
         "finding": f"correspondent's charge policy: {levied_charges}",
         "explainsGap": gap is not None and abs(gap) in {_num(c) for c in levied_charges}},
    ]
    explained_by = [c["check"] for c in checks if c["explainsGap"]]
    unexplained = gap not in (None, 0) and not explained_by
    return {
        "expectedAmount": expected, "bookedAmount": booked, "gap": gap,
        "checks": checks, "explainedBy": explained_by, "unexplained": unexplained,
        "escalation": {
            "thresholdAmount": threshold,
            "rule": ("Internal escalation rule: an unexplained gap above the threshold is "
                     "escalated to the correspondent. It triggers escalation; it does not "
                     "establish the cause."),
            "required": unexplained and abs(gap) > threshold,
        },
    }


def discrepancy_checks_for(db: Any, payment_id: str, bic: Optional[str]) -> Optional[dict]:
    payment = db["payments"].find_one({"paymentId": payment_id}, {"_id": 0}) or {}
    position = _latest_position(db, payment_id)
    if position is None:
        return None
    entry = statement_entry(db, payment_id, position)
    return discrepancy_checks(payment, position, entry, known_charges(db, bic))
