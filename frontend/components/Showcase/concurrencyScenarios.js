// Data integrity under concurrency: scenarios that race real writes against one invariant.
// Keys must not clash with R*, TL or C* (ShowcaseView resolves `?scenario=` across all sets).

export const CONCURRENCY_SCENARIOS = [
  {
    key: "W1",
    mode: "integrity",
    decision: {
      cause: { label: "Three payments at once", code: "RACE" },
      constraint: { label: "Balance never below zero", code: "NON_NEGATIVE" },
      action: { label: "Guard admits exactly two", code: "GUARDED_DEBIT" },
    },
    title: "Balance never goes negative",
    story:
      "Three $80 payments hit a $200 account at the same instant. Every payment reads $200, so every one looks affordable. A guard in the update's filter on the account document lets exactly two through.",
    invariant: "balance.available >= 0",
    cardAmount: "3 × USD 80 vs USD 200",
  },
];

// The walkthrough's step list. The race is three real wires through the payment saga; the steps
// after it settle and reconcile the two that got through, the way the reconciliation scenarios do.
export const CONCURRENCY_STEPS = [
  {
    key: "queue",
    title: "Queue three wires",
    button: "Queue three wires",
    summary:
      "Three USD 80 wires leave one customer account at the same moment. At stage 3 each must reserve funds on that account before it can be routed, so they all contend for one balance.",
    beat: "The account holds USD 200, which covers two wires. Exactly two should get through.",
  },
  {
    key: "race",
    title: "Race the wires through the saga",
    button: "Send three wires at once",
    summary:
      "Each wire runs the real payment saga. They are released into the stage 3 reserve together, and the balance check is the filter of one conditional update on the shared account document. The winners go on to be debited at stage 5.",
    beat: "Every wire reads USD 200, but the guard ignores that read. Racing writers collide on the same document, MongoDB raises a write conflict, the loser retries on fresh state, and the guard refuses once the money is gone.",
  },
  {
    key: "settle",
    title: "The correspondent settles",
    button: "Settle the wires",
    summary:
      "The correspondent bank confirms it has settled the funds (stage 7). Each winning wire flips to SETTLED, and a change stream tells the ledger.",
    beat: "Two wires settle. The refused wire never left the bank, so there is nothing to settle.",
  },
  {
    key: "glpost",
    title: "The books are written",
    button: "Post to the ledger",
    summary:
      "The general-ledger batch journals each settled wire into balanced double-entry postings. As far as our own books go, the wires are done.",
    beat: "Two journals post, each debiting the customer and crediting our nostro account.",
  },
  {
    key: "statement",
    title: "The correspondent's statement arrives",
    button: "Receive the statement",
    summary:
      "The correspondent sends its own record of every movement on our nostro account (camt.053). Reconciliation checks our books against it.",
    beat: "A clean statement: both lines carry the right amount.",
  },
  {
    key: "match",
    title: "Reconciliation compares the two",
    button: "Reconcile",
    summary:
      "The next batch matches each statement line to a payment and runs a three-way comparison of payment, ledger and statement. When all three agree, the payment is RECONCILED.",
    beat: "Both wires match to the cent and reconcile with no exception.",
  },
  {
    key: "verify",
    title: "Verify the result",
    final: true,
    summary: "Check the balance and what became of every wire.",
    beat: "Two wires reserved, debited, settled and reconciled. One refused. Available ends at USD 40 and never went negative.",
  },
];

export const CONCURRENCY_GROUP = {
  mode: "integrity",
  icon: "Lock",
  title: "Data integrity under concurrency",
  blurb: "Real writes race one invariant on MongoDB. No agent: the database decides.",
};

export const concurrencyByKey = (key) => CONCURRENCY_SCENARIOS.find((s) => s.key === key) || null;
