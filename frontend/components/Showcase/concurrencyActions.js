import { coreApi, pipelineApi } from "@/lib/api/client";

// Every call resolves with { data, error }, the client helpers' shape. The stepper decides what
// an error means. The race is three real wires through the saga, so it takes tens of seconds.
export const runRace = () => coreApi("workflow/demo/concurrency/payments", { method: "POST" });

/** The correspondent confirms settlement for every captured wire (stage 7). */
export const settleDue = () => coreApi("workflow/demo/settle-due", { method: "POST" });

/** The ledger's GL batch: journals settled wires, and matches statement lines on the next run. */
export const runLedgerBatch = () => pipelineApi("batch/trigger", null, { method: "POST" });

/** The correspondent's camt.053 for our nostro. `generated` is false when no wire is waiting. */
export const bookStatement = () =>
  coreApi("FinancialGateway/GW-WIRE-01/Statement/Generate", {
    method: "POST",
    body: { accountCode: "1111", includeOrphan: false },
  });

export const getPayment = (paymentId) => coreApi(`workflow/payments/${encodeURIComponent(paymentId)}`);
