// Per-scenario brief: what a viewer needs before the run starts. Text is grounded in the
// scenario cards and the backend policy; nothing here is read from the live run.
//   autonomy.level: index into AUTONOMY_LEVELS
//   autonomy.alone / needsYou: what the agent does by itself, and what waits for a human

export const AUTONOMY_LEVELS = [
  { label: "Observe", hint: "Watches and reports" },
  { label: "Plan and propose", hint: "Investigates, then proposes" },
  { label: "Act with confirmation", hint: "Acts once a human approves" },
  { label: "Act autonomously", hint: "Acts without asking" },
];

const MONGO_RECON = ["ACID transactions", "Change streams", "Embedded agent record", "LangGraph checkpoints"];
const MONGO_CUTOFF = ["Change streams", "Time series stage events", "$percentile timing", "LangGraph checkpoints"];

export const BRIEFS = {
  R1: {
    hard: "The statement is 25.00 short. Whether that is our loss depends on who agreed to pay the charges.",
    decides: "Whether the gap is a fee, and what to do about it.",
    guarantees: "Under sender-pays, code allows only a posting adjustment, and its amount must equal the gap exactly.",
    mongo: [...MONGO_RECON, "Idempotent ledger write"],
    autonomy: { level: 2, alone: "Reads the history, finds the cause, proposes the fix", needsYou: "Posting the adjustment" },
  },
  R1b: {
    hard: "The same 25.00 gap as the sender-pays case, but the books must end up different.",
    decides: "Whether the gap is a fee, and which charge terms apply.",
    guarantees: "Under shared charges, code allows only accepting the gap. No ledger entry is written.",
    mongo: MONGO_RECON,
    autonomy: { level: 2, alone: "Reads the history, finds the cause, proposes accepting it", needsYou: "Accepting the discrepancy" },
  },
  R2: {
    hard: "Neither side matches on its own: one missing line and one unknown line open two exceptions for one cause.",
    decides: "Whether the unknown statement line is our payment under a different reference.",
    guarantees: "A fuzzy match is never automatic. Linking needs approval, and one link closes both exceptions.",
    mongo: MONGO_RECON,
    autonomy: { level: 2, alone: "Searches the statement for candidates and proposes a link", needsYou: "Linking the statement line" },
  },
  R4: {
    hard: "The statement is 54.00 short, and no correspondent charges a fee of that size.",
    decides: "Whether to trust a fee explanation or ask the correspondent.",
    guarantees: "Code refuses a fee explanation unless the amount is a charge the correspondent actually levies.",
    mongo: MONGO_RECON,
    autonomy: { level: 2, alone: "Declines to guess and proposes asking the correspondent", needsYou: "Sending the query" },
  },
  R5: {
    hard: "The statement holds a line that belongs to no payment of ours.",
    decides: "Whether any of our payments could explain the line.",
    guarantees: "With no candidate, policy allows only escalating to the correspondent or dismissing the line.",
    mongo: MONGO_RECON,
    autonomy: { level: 2, alone: "Searches for candidates, finds none, proposes escalation", needsYou: "Sending the query" },
  },
  TL: {
    hard: "Nothing is wrong yet. The wire is fine and the correspondent's statement has not arrived.",
    decides: "When to look again.",
    guarantees: "Re-checking moves no money and changes no books, so it is the only action allowed without approval.",
    mongo: ["Change streams", "Scheduled re-checks", "Embedded agent record"],
    autonomy: { level: 3, alone: "Re-runs reconciliation on a schedule", needsYou: "Nothing, unless the line never arrives" },
  },
  C1: {
    hard: "Time is the constraint. A second signature is missing, the approver is out, and the Fedwire cut-off is closing.",
    decides: "When to remind, when to escalate to the backup, and when to ask to expedite.",
    guarantees: "Expediting needs a signatory's approval. The agent cannot approve its own request.",
    mongo: MONGO_CUTOFF,
    autonomy: { level: 2, alone: "Reminds the approver, escalates to the backup, records its assessment", needsYou: "Expediting the wire" },
  },
  C3: {
    hard: "The account is short of funds and no incoming credit will cover it before the cut-off.",
    decides: "Whether the wire can make today's cut-off at all.",
    guarantees: "Moving the wire to another day needs approval. Telling the customer does not.",
    mongo: MONGO_CUTOFF,
    autonomy: { level: 2, alone: "Tells the customer about the shortfall, records its assessment", needsYou: "Holding the wire for the next value date" },
  },
};

export const briefFor = (key) => BRIEFS[key] || null;
