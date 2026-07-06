// Quick-preset transactions — ported from .docs/gl-monitor-static-ref/js/initiate.js
// and backend/ledger/static/gl_monitor.html. `mode` drives the posting path:
// WIRE/INTERNAL rails post in real time; ACH/VENMO/PAYPAL rails post via the
// scheduled gl_batch. The UI groups presets by this field.
export const PRESETS = [
  { mode: "REALTIME", label: "Grace → Monet $50",   customerId: "CUST-00528224", type: "CREDIT_TRANSFER",   rail: "INTERNAL", debtor: "ACC-e0583b3b", creditor: "ACC-e0583b3c", amount: 50,  remittance: "Ledger smoke test 1" },
  { mode: "REALTIME", label: "Grace → Monet $125",  customerId: "CUST-00528224", type: "CREDIT_TRANSFER",   rail: "INTERNAL", debtor: "ACC-e0583b3b", creditor: "ACC-8c8097a4", amount: 125, remittance: "Ledger smoke test 2" },
  { mode: "REALTIME", label: "Grace own-acct $200", customerId: "CUST-00528224", type: "INTRABANK_TRANSFER", rail: "INTERNAL", debtor: "ACC-e0583b3b", creditor: "ACC-e0583b3f", amount: 200, remittance: "" },
  { mode: "REALTIME", label: "Monet → Grace $75",   customerId: "CUST-17352703", type: "CREDIT_TRANSFER",   rail: "INTERNAL", debtor: "ACC-e0583b3c", creditor: "ACC-e0583b3b", amount: 75,  remittance: "Ledger smoke test 4" },
  { mode: "REALTIME", label: "Monet → Grace $30",   customerId: "CUST-17352703", type: "CREDIT_TRANSFER",   rail: "INTERNAL", debtor: "ACC-e0583b3c", creditor: "ACC-e0583b3f", amount: 30,  remittance: "Ledger smoke test 5" },
  { mode: "BATCH",    label: "Grace → Monet $60",   customerId: "CUST-00528224", type: "CREDIT_TRANSFER",   rail: "ACH",      debtor: "ACC-e0583b3b", creditor: "ACC-e0583b3c", amount: 60,  remittance: "Batch smoke test 1" },
  { mode: "BATCH",    label: "Grace → Monet $150",  customerId: "CUST-00528224", type: "CREDIT_TRANSFER",   rail: "ACH",      debtor: "ACC-e0583b3b", creditor: "ACC-8c8097a4", amount: 150, remittance: "Batch smoke test 2" },
  { mode: "BATCH",    label: "Monet → Grace $45",   customerId: "CUST-17352703", type: "CREDIT_TRANSFER",   rail: "ACH",      debtor: "ACC-e0583b3c", creditor: "ACC-e0583b3b", amount: 45,  remittance: "Batch smoke test 3" },
  { mode: "BATCH",    label: "Monet → Grace $90",   customerId: "CUST-17352703", type: "CREDIT_TRANSFER",   rail: "ACH",      debtor: "ACC-e0583b3c", creditor: "ACC-e0583b3f", amount: 90,  remittance: "Batch smoke test 4" },
];
