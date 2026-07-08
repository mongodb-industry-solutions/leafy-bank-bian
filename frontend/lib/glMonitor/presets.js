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
