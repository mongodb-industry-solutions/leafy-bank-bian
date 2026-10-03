// Stage 9 step 4 — apply the deferred idempotency unique index (doc 24 §3 step 4).
//
// The index is INERT today: no caller sends an idempotency key, so the field is always null
// and a sparse index constrains nothing. Applying it closes the stage-1 R3 deferral
// ("end of the stage sequence" — this is it) and makes capture.py's DuplicateKeyError handler
// the real guarantee under concurrent identical-key submissions.
//
// Also drops the dead `idx_end_to_end_id_unique` if it still survives — a safe no-op when
// absent (the step-0 precondition check confirmed it is already gone on fsi-bian-test-db).
//
// Run (MONGODB_URI in your terminal env — same convention as the verifiers):
//   export MONGODB_URI="mongodb+srv://..."
//   export LEAFYBANK_DB_NAME=fsi-bian-test-db        # your dev DB; Kanopy: leafy-bank-bian
//   mongosh "$MONGODB_URI" backend/data/apply_stage4_index.js
//
// Idempotent: re-running is a no-op once the index matches. Does NOT seed data.

const dbName = process.env.LEAFYBANK_DB_NAME || "fsi-bian-test-db";
const dbc = db.getSiblingDB(dbName);

// 1. Drop any prior idx_idempotency_key_unique — the first attempt used `sparse: true`,
// which collides on nested-null (payment_document.build initialises idempotency.idempotencyKey
// to null). `createIndex` will NOT overwrite an existing index whose spec differs, so drop
// first, then re-create with the partialFilterExpression below.
const existing = dbc.payments.getIndexes().map((i) => i.name);
if (existing.includes("idx_idempotency_key_unique")) {
  dbc.payments.dropIndex("idx_idempotency_key_unique");
  print("dropped prior idx_idempotency_key_unique (sparse → partial)");
}

// 2. Apply the unique PARTIAL index. partialFilterExpression on `$type: "string"`
// indexes only docs where the key is an actual string — null and missing are excluded,
// so payments without a retry key never collide. (sparse on a nested path does NOT
// exclude an explicit-null leaf, which is why the sparse version 500'd on Initiate.)
dbc.payments.createIndex(
  { "idempotency.idempotencyKey": 1 },
  {
    name: "idx_idempotency_key_unique",
    unique: true,
    partialFilterExpression: { "idempotency.idempotencyKey": { $type: "string" } },
  }
);

// 3. Drop the dead endToEnd index if it still exists (safe no-op if absent).
if (existing.includes("idx_end_to_end_id_unique")) {
  dbc.payments.dropIndex("idx_end_to_end_id_unique");
  print("dropped dead index: idx_end_to_end_id_unique");
}

// 4. Report.
print(`payments indexes on ${dbName}:`);
dbc.payments.getIndexes().forEach((i) => {
  const opts = [];
  if (i.unique) opts.push("unique");
  if (i.sparse) opts.push("sparse");
  if (i.partialFilterExpression) opts.push("partial");
  print(`  ${i.name}${opts.length ? " (" + opts.join(", ") + ")" : ""}`);
});
print("STAGE 4: idx_idempotency_key_unique applied (partial, $type:string).");
