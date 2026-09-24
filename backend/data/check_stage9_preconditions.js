// Stage 9 step-0 preconditions (doc 24 §3 step 0). READ-ONLY — safe to run any time.
//
//   mongosh "$MONGODB_URI" --quiet backend/data/check_stage9_preconditions.js
//
// Three checks:
//   [1] `exceptions` must NOT already exist on the dev DB (ownership — defect
//       2026-08-31 repo-is-not-the-database: the repo is not the database).
//   [2] no duplicate non-null `idempotency.idempotencyKey` values — the unique sparse
//       index (step 4) cannot be applied over dupes. Also reports the legacy FLAT
//       `idempotencyKey` (pre-2026-09-22 relocation) in case stale docs carry it.
//   [3] current `payments` indexes — whether `idx_idempotency_key_unique` is already
//       applied and whether the dead `idx_end_to_end_id_unique` is still there to drop.

const DB_NAME = "fsi-bian-test-db";
const dbx = db.getSiblingDB(DB_NAME);

// [1] ownership
const colls = dbx.getCollectionNames();
print(`[1] ${DB_NAME} holds ${colls.length} collections`);
print(`    exceptions present: ${colls.includes("exceptions")}`);

// [2] idempotency-key dedupe
const keyed = dbx.payments.countDocuments({ "idempotency.idempotencyKey": { $ne: null } });
print(`[2] payments with idempotency.idempotencyKey set: ${keyed}`);
const dupes = dbx.payments.aggregate([
  { $match: { "idempotency.idempotencyKey": { $ne: null } } },
  { $group: { _id: "$idempotency.idempotencyKey", n: { $sum: 1 }, ids: { $push: "$paymentId" } } },
  { $match: { n: { $gt: 1 } } },
]).toArray();
print(`    duplicate keys: ${dupes.length}`);
dupes.forEach((d) => print(`    DUPE ${d._id} x${d.n} -> ${d.ids.join(", ")}`));
const flatKeyed = dbx.payments.countDocuments({ idempotencyKey: { $ne: null } });
print(`    payments with legacy flat idempotencyKey: ${flatKeyed}`);

// [3] index state
const idx = dbx.payments.getIndexes().map((i) => i.name);
print(`[3] payments indexes: ${idx.join(", ")}`);
print(`    idx_idempotency_key_unique applied: ${idx.includes("idx_idempotency_key_unique")}`);
print(`    idx_end_to_end_id_unique still present (dead, drop at step 4): ${idx.includes("idx_end_to_end_id_unique")}`);
