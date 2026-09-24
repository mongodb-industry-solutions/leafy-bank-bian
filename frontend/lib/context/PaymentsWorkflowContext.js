"use client";

import React, { createContext, useContext, useMemo, useState } from "react";

/**
 * Holds the Payments Workflow surface's active lens — "payments" (initiation + watch the
 * just-created payment's lifecycle in place), "activity" (the historical list), or
 * "operations" (the Stage 9 exceptions queue — `PaymentsLens` rendered with `exceptionsOnly`,
 * doc 24 §3 step 8).
 *
 * Lives at the AppShell so the NavBar top bar and the /payments-workflow page share one
 * source of truth without URL params (which would force every route through the root
 * layout into dynamic rendering). The NavBar renders the lens switches; the page reads
 * `lens` to decide what to render.
 */
const PaymentsWorkflowContext = createContext(null);

export const WORKFLOW_LENS = {
  PAYMENTS: "payments",
  ACTIVITY: "activity",
  OPERATIONS: "operations",
};

export function PaymentsWorkflowProvider({ children }) {
  const [lens, setLens] = useState(WORKFLOW_LENS.ACTIVITY);
  const value = useMemo(() => ({ lens, setLens }), [lens]);
  return (
    <PaymentsWorkflowContext.Provider value={value}>
      {children}
    </PaymentsWorkflowContext.Provider>
  );
}

export function usePaymentsWorkflow() {
  const ctx = useContext(PaymentsWorkflowContext);
  if (!ctx) {
    throw new Error("usePaymentsWorkflow must be used within PaymentsWorkflowProvider");
  }
  return ctx;
}
