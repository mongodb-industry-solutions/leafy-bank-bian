"use client";

import { useState } from "react";
import Button from "@leafygreen-ui/button";
import { Body, Overline } from "@leafygreen-ui/typography";
import { friendlyError, retryInvestigation } from "../agentActions";
import styles from "../Showcase.module.css";

/** The agent hit an error (the worker retries on its own); lets the presenter retry at once. */
export default function AgentFailure({ error, exceptionId, paymentId }) {
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState(null);

  async function retry() {
    setRetrying(true);
    setRetryError(null);
    const { error: err } = await retryInvestigation(exceptionId, paymentId);
    setRetrying(false);
    if (err) setRetryError(friendlyError(err));
  }

  return (
    <div className={styles.findingCard}>
      <Overline>Investigation failed</Overline>
      <Body className={styles.error}>{error.message}</Body>
      <Body className={styles.muted}>
        Retrying automatically (attempt {error.attempts}). If this is an expired AWS SSO
        session, run <span className={styles.mono}>aws sso login</span> first.
      </Body>
      <Button size="small" disabled={retrying} onClick={retry}>
        {retrying ? "Retrying…" : "Retry investigation"}
      </Button>
      {retryError && <Body className={styles.error}>{retryError}</Body>}
    </div>
  );
}
