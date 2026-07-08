// Given a clicked card and the four already-loaded columns, compute the set of
// linked doc ids in every column — the full flow of one payment across stages.
//
// Join keys (all present in loaded data, no fetch needed):
//   transaction.paymentId            == ledgerEvent.idempotencyKey
//   ledgerEvent.eventId              == subLedgerEntry.sourceReference.sourceId
//   subLedgerEntry.journalEntryId    == journalEntry.journalId
//
// A journal aggregates many payments, so clicking a journal fans in to every
// subledger entry it contains (+ their events + payments); clicking any earlier
// stage resolves the single chain that card belongs to.

const empty = () => ({ tx: new Set(), le: new Set(), sl: new Set(), jn: new Set() });

// The id used to match a row against the lineage set, per column.
export const linkKeyOf = {
  tx: (t) => t.paymentId,
  le: (e) => e.eventId,
  sl: (s) => s.subLedgerId,
  jn: (j) => j.journalId,
};

export function computeLineage(col, item, columns) {
  const res = empty();
  if (!item) return res;
  const le = columns.le.items || [];
  const sl = columns.sl.items || [];

  // Journal → fan in to all its members.
  if (col === "jn") {
    const jid = item.journalId;
    if (!jid) return res;
    res.jn.add(jid);
    const eventIds = new Set();
    sl.forEach((s) => {
      if (s.journalEntryId === jid) {
        res.sl.add(s.subLedgerId);
        const ev = s.sourceReference?.sourceId;
        if (ev) eventIds.add(ev);
      }
    });
    le.forEach((e) => {
      if (eventIds.has(e.eventId)) {
        res.le.add(e.eventId);
        if (e.idempotencyKey) res.tx.add(e.idempotencyKey);
      }
    });
    return res;
  }

  // tx / le / sl → resolve the single chain via a paymentId + eventId anchor.
  let paymentId = null;
  let eventId = null;
  if (col === "tx") {
    paymentId = item.paymentId;
    eventId = le.find((e) => e.idempotencyKey === paymentId)?.eventId ?? null;
  } else if (col === "le") {
    eventId = item.eventId;
    paymentId = item.idempotencyKey ?? null;
  } else if (col === "sl") {
    eventId = item.sourceReference?.sourceId ?? null;
    paymentId = le.find((e) => e.eventId === eventId)?.idempotencyKey ?? null;
  }

  if (paymentId) res.tx.add(paymentId);
  if (eventId) res.le.add(eventId);

  let journalId = col === "sl" ? item.journalEntryId || null : null;
  sl.forEach((s) => {
    if (eventId && s.sourceReference?.sourceId === eventId) {
      res.sl.add(s.subLedgerId);
      if (s.journalEntryId) journalId = s.journalEntryId;
    }
  });
  if (col === "sl") res.sl.add(item.subLedgerId);
  if (journalId) res.jn.add(journalId);

  return res;
}
