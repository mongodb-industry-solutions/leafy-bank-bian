"use client";

import { useState } from "react";
import Button from "@leafygreen-ui/button";
import { Body, Overline } from "@leafygreen-ui/typography";
import { decideProposal, friendlyError } from "../agentActions";
import { major } from "./format";
import styles from "../Showcase.module.css";

// What approving will write, per action. Only actions the backend routes actually perform.
function effectOf(proposal, bank) {
  switch (proposal.action) {
    case "POST_ADJUSTMENT":
      return `A correcting journal: Dr 5214 correspondent charges / Cr 1111 nostro, ${major(proposal.params?.amount)}.`;
    case "ACCEPT_DISCREPANCY":
      return "No journal. The exception closes and the payment reconciles with the gap accepted.";
    case "LINK_STATEMENT_ENTRY":
      return "The statement line is linked to the payment. One link closes both exceptions.";
    case "ESCALATE_TO_CORRESPONDENT":
      return `A camt.026 investigation request goes to ${bank}. The books stay untouched.`;
    default:
      return null;
  }
}

/** Act 4: the climax. Approve and Reject are the primary controls. */
export default function DecisionScene({ scenario, exceptionId, proposal, canDecide, onDecided }) {
  const [acting, setActing] = useState(null);
  const [error, setError] = useState(null);

  async function decide(decision) {
    if (acting) return;
    setActing(decision);
    setError(null);
    const { error: err } = await decideProposal(exceptionId, decision);
    setActing(null);
    if (err) {
      setError(friendlyError(err));
      return;
    }
    onDecided(decision);
  }

  if (!proposal) return <Body className={styles.muted}>No proposal to decide on.</Body>;
  const effect = effectOf(proposal, scenario.bank);

  return (
    <div className={styles.section}>
      <div className={styles.proposal}>
        <Overline>The agent proposes</Overline>
        <Body className={styles.proposalAction}>
          {scenario.decision.action.label}
          <span className={`${styles.muted} ${styles.mono}`}> · {proposal.action}</span>
        </Body>
        {effect && (
          <>
            <Overline>Approving writes</Overline>
            <Body>{effect}</Body>
          </>
        )}
        {proposal.rationale && (
          <>
            <Overline>Why</Overline>
            <Body className={styles.muted}>{proposal.rationale}</Body>
          </>
        )}
        <Body className={styles.policy}>Policy: {scenario.policyLine}</Body>
      </div>
      {canDecide && (
        <div className={styles.actions}>
          <Button variant="primary" size="large" disabled={!!acting} onClick={() => decide("APPROVE")}>
            {acting === "APPROVE" ? "Executing…" : "Approve"}
          </Button>
          <Button size="large" disabled={!!acting} onClick={() => decide("REJECT")}>
            {acting === "REJECT" ? "Rejecting…" : "Reject"}
          </Button>
        </div>
      )}
      {error && <Body className={styles.error}>{error}</Body>}
    </div>
  );
}
