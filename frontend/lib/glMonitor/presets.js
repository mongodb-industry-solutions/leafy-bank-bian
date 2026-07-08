// Quick-preset transactions — ported from .docs/gl-monitor-static-ref/js/initiate.js
// and backend/ledger/static/gl_monitor.html. `mode` drives the posting path:
// WIRE/INTERNAL rails post in real time; ACH/VENMO/PAYPAL rails post via the
// scheduled gl_batch. The UI groups presets by this field.
export const PRESETS = [
  { mode: "REALTIME", label: "Grace → Monet $50",   customerId: "CUST-00528224", type: "CREDIT_TRANSFER",   rail: "INTERNAL", debtor: "ACC-e0583b3b", creditor: "ACC-e0583b3c", amount: 50,  remittance: "Ledger smoke test 1" },
  { mode: "REALTIME", label: "Monet → Grace $75",   customerId: "CUST-17352703", type: "CREDIT_TRANSFER",   rail: "INTERNAL", debtor: "ACC-e0583b3c", creditor: "ACC-e0583b3b", amount: 75,  remittance: "Ledger smoke test 4" },
  { mode: "REALTIME", label: "Frida → Grace $80",   customerId: "CUST-f88fb89e", type: "CREDIT_TRANSFER",   rail: "INTERNAL", debtor: "ACC-e0583b3a", creditor: "ACC-e0583b3f", amount: 80,  remittance: "Ledger smoke test 6" },
  { mode: "REALTIME", label: "Frida → Monet $110",  customerId: "CUST-f88fb89e", type: "CREDIT_TRANSFER",   rail: "INTERNAL", debtor: "ACC-e0583b3a", creditor: "ACC-e0583b3c", amount: 110, remittance: "Ledger smoke test 7" },
  { mode: "REALTIME", label: "Frida own-acct $250", customerId: "CUST-f88fb89e", type: "INTRABANK_TRANSFER", rail: "INTERNAL", debtor: "ACC-e0583b3a", creditor: "ACC-e0583b39", amount: 250, remittance: "" },
  { mode: "BATCH",    label: "Grace → Monet $60",   customerId: "CUST-00528224", type: "CREDIT_TRANSFER",   rail: "ACH",      debtor: "ACC-e0583b3b", creditor: "ACC-e0583b3c", amount: 60,  remittance: "Batch smoke test 1" },
  { mode: "BATCH",    label: "Monet → Grace $45",   customerId: "CUST-17352703", type: "CREDIT_TRANSFER",   rail: "ACH",      debtor: "ACC-e0583b3c", creditor: "ACC-e0583b3b", amount: 45,  remittance: "Batch smoke test 3" },
  { mode: "BATCH",    label: "Frida → Grace $65",   customerId: "CUST-f88fb89e", type: "CREDIT_TRANSFER",   rail: "ACH",      debtor: "ACC-e0583b3a", creditor: "ACC-e0583b3f", amount: 65,  remittance: "Batch smoke test 5" },
  { mode: "BATCH",    label: "Frida → Monet $140",  customerId: "CUST-f88fb89e", type: "CREDIT_TRANSFER",   rail: "ACH",      debtor: "ACC-e0583b3a", creditor: "ACC-e0583b3c", amount: 140, remittance: "Batch smoke test 6" },
];

// Seeded customer accounts (accountId → owning customerId), used to synthesize a
// randomized bulk batch. Amounts are kept small so several debits from the same
// account within one batch stay within the seeded balances.
const BULK_ACCOUNTS = [
  { accountId: "ACC-e0583b3b", customerId: "CUST-00528224" }, // Grace savings
  { accountId: "ACC-e0583b3f", customerId: "CUST-00528224" }, // Grace checking
  { accountId: "ACC-e0583b3c", customerId: "CUST-17352703" }, // Monet checking
  { accountId: "ACC-8c8097a4", customerId: "CUST-17352703" }, // Monet savings
  { accountId: "ACC-e0583b3d", customerId: "CUST-17352703" }, // Monet savings 2
  { accountId: "ACC-e0583b3a", customerId: "CUST-f88fb89e" }, // Frida checking
  { accountId: "ACC-e0583b39", customerId: "CUST-f88fb89e" }, // Frida savings
  { accountId: "ACC-e0583b3e", customerId: "CUST-f88fb89e" }, // Frida savings 2
  { accountId: "ACC-205afd64", customerId: "CUST-f88fb89e" }, // Frida savings 3
];

const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

// Build a randomized batch of valid payment payloads for BulkInitiate. Each item
// picks a debtor, a distinct creditor, derives type from same-customer ownership,
// randomizes the rail (INTERNAL realtime vs ACH batch) so both pipeline paths get
// exercised, and keeps amounts modest ($5–$50, under PAYMENT_LIMIT_USD).
export function generateBulkItems(n = 5) {
  const items = [];
  for (let i = 0; i < n; i++) {
    const debtor = pick(BULK_ACCOUNTS);
    let creditor = pick(BULK_ACCOUNTS);
    while (creditor.accountId === debtor.accountId) creditor = pick(BULK_ACCOUNTS);
    const isInternal = debtor.customerId === creditor.customerId;
    const amount = Math.round((5 + Math.random() * 45) * 100) / 100;
    items.push({
      customerId: debtor.customerId,
      type: isInternal ? "INTRABANK_TRANSFER" : "CREDIT_TRANSFER",
      rail: pick(["INTERNAL", "ACH"]),
      debtor: { accountId: debtor.accountId },
      creditor: { accountId: creditor.accountId },
      instructedAmount: amount,
      instructedCurrency: "USD",
      remittance: { unstructured: `Bulk batch #${i + 1}` },
    });
  }
  return items;
}
