// Turns one payment's recorded MongoDB commands (concurrency_demo `ops`) into the steps shown for
// that payment, grouped by transaction. Nothing here is scripted: every step is a command the
// driver sent, with the result the server returned. Only the wording around each command is ours.

import { usd } from "./concurrencyRunLog";

const json = (value) => JSON.stringify(value);

// The reserve and the debit also stamp two timestamps; they are noise next to the balance change.
const stripStamps = (update) => (update?.$inc ? { $inc: update.$inc } : update);

/** What a recorded command is, from its shape. */
export function kindOf(op) {
  const inc = op.update?.$inc || {};
  if (op.command === "commitTransaction") return "commit";
  if (op.command === "abortTransaction") return "abort";
  if (op.command === "insert") return "txn";
  if (op.collection === "payments") return op.command === "update" ? "marker" : "releaseMarker";
  if (op.command === "update") return "release";
  if (inc["balance.hold"] > 0) return "reserve";
  if (inc["balance.current"] < 0) return "debit";
  return "credit";
}

function snippet(op) {
  if (op.command === "insert") return `db.${op.collection}.insert_one({ "paymentId": "${op.filter.paymentId}", ... }, session=session)`;
  if (op.command === "commitTransaction") return "session.commit_transaction()";
  if (op.command === "abortTransaction") return "session.abort_transaction()";
  const update = json(stripStamps(op.update));
  const verb = op.command === "update" ? "update_one" : "find_one_and_update";
  return `db.${op.collection}.${verb}(\n  ${json(op.filter)},\n  ${update},\n  session=session)`;
}

const guarded = (amount) => ({
  MATCHED: { tone: "ok", note: `The filter matched, so the balance moved in one atomic step.` },
  WRITE_CONFLICT: { tone: "wait", note: "Write conflict (112): another transaction changed this document first. This one aborts and retries." },
  NO_MATCH: { tone: "refuse", note: `No match: available is below ${usd(amount)}, so nothing was written.` },
});

function describe(op, prev, amount) {
  const base = { code: snippet(op), result: op.result };
  switch (kindOf(op)) {
    case "marker":
      return { ...base, tone: "ok", title: "Mark the payment as reserved", note: "Set first, in the same transaction, so a replay cannot decrement the balance twice." };
    case "reserve":
      return { ...base, title: "Guarded update on the account", open: true, ...(guarded(amount)[op.result] || { tone: "info", note: "" }) };
    case "release":
      return {
        ...base, title: "Release the hold",
        ...(op.result === "WRITE_CONFLICT"
          ? { tone: "wait", note: "Write conflict (112): another payment touched the account first. Abort and retry." }
          : { tone: "ok", note: "The hold returns to available; the debit follows in the same transaction." }),
      };
    case "releaseMarker":
      return { ...base, tone: "ok", title: "Mark the reservation released", note: "Flips the payment's marker so the release cannot run twice." };
    case "debit":
      return {
        ...base, title: "Debit with the same $gte guard", open: true,
        ...(op.result === "NO_MATCH"
          ? { tone: "refuse", note: "Insufficient funds at settlement time. The whole transaction rolls back." }
          : op.result === "WRITE_CONFLICT"
            ? { tone: "wait", note: "Write conflict (112). Abort and retry on fresh state." }
            : { tone: "ok", note: `Current, available and ledger fall by ${usd(amount)} in one atomic step.` }),
      };
    case "credit":
      return { ...base, tone: "ok", title: "Credit the receiving account", note: "For an external wire this is the clearing account, in the same transaction." };
    case "txn":
      return { ...base, tone: "ok", title: "Insert the transactions record", note: "The record of the money move lands with the balance changes." };
    case "commit":
      return { ...base, tone: "ok", title: "Commit", note: "Every change in this transaction lands together." };
    default: {
      const conflict = prev?.result === "WRITE_CONFLICT";
      return {
        ...base, tone: conflict ? "wait" : "refuse", title: conflict ? "Abort, retry on fresh state" : "Abort, payment refused",
        note: conflict
          ? "The driver reruns the transaction against the document as it is now, where the filter sees the true balance."
          : "The reservation marker rolls back too, so the refused payment leaves no trace.",
      };
    }
  }
}

/** Groups by transaction and names each stage: 3 when it reserves, 5 when it moves the money. */
export function groupsOf(attempt, amount) {
  const byTxn = new Map();
  attempt.ops.forEach((op, i) => {
    const key = op.attempt ?? "none";
    if (!byTxn.has(key)) byTxn.set(key, []);
    byTxn.get(key).push({ op, prev: attempt.ops[i - 1] });
  });
  const groups = [...byTxn.values()].map((items) => {
    const reserves = items.some(({ op }) => ["marker", "reserve"].includes(kindOf(op)));
    return { stage: reserves ? 3 : 5, steps: items.map(({ op, prev }) => describe(op, prev, amount)) };
  });
  const perStage = { 3: groups.filter((g) => g.stage === 3).length, 5: groups.filter((g) => g.stage === 5).length };
  const seen = { 3: 0, 5: 0 };
  return groups.map((g) => {
    seen[g.stage] += 1;
    const name = g.stage === 3 ? "Stage 3 · reserve funds" : "Stage 5 · move the money";
    return { ...g, label: perStage[g.stage] > 1 ? `${name} · try ${seen[g.stage]}` : name };
  });
}

/** Total steps, the read step included, so the reveal counter knows when every card is done. */
export const stepCount = (attempt, amount) =>
  1 + groupsOf(attempt, amount).reduce((n, g) => n + g.steps.length, 0);
