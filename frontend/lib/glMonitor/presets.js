// Quick-preset transactions — ported from .docs/gl-monitor-static-ref/js/initiate.js.
export const PRESETS = [
  { label: "Grace → Monet $50",   customerId: "CUST-00528224", type: "CREDIT_TRANSFER",   rail: "INTERNAL", debtor: "ACC-e0583b3b", creditor: "ACC-e0583b3c", amount: 50,  remittance: "Ledger smoke test 1" },
  { label: "Grace → Monet $125",  customerId: "CUST-00528224", type: "CREDIT_TRANSFER",   rail: "INTERNAL", debtor: "ACC-e0583b3b", creditor: "ACC-8c8097a4", amount: 125, remittance: "Ledger smoke test 2" },
  { label: "Grace own-acct $200", customerId: "CUST-00528224", type: "INTRABANK_TRANSFER", rail: "INTERNAL", debtor: "ACC-e0583b3b", creditor: "ACC-e0583b3f", amount: 200, remittance: "" },
  { label: "Monet → Grace $75",   customerId: "CUST-17352703", type: "CREDIT_TRANSFER",   rail: "INTERNAL", debtor: "ACC-e0583b3c", creditor: "ACC-e0583b3b", amount: 75,  remittance: "Ledger smoke test 4" },
  { label: "Monet → Grace $30",   customerId: "CUST-17352703", type: "CREDIT_TRANSFER",   rail: "INTERNAL", debtor: "ACC-e0583b3c", creditor: "ACC-e0583b3f", amount: 30,  remittance: "Ledger smoke test 5" },
];
