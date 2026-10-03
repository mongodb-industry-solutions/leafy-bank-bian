"use client";

// Plan E: the agent scenario walkthrough (reconciliation today; the cutoff agent joins later). Picker → stepper + narration → two panes
// (payment tracker, agent thinking). The step machine and its polls live in ScenarioStepper.

import { useEffect, useState } from "react";
import Banner from "@leafygreen-ui/banner";
import LeafyGreenProvider from "@leafygreen-ui/leafygreen-provider";
import { H2, Body } from "@leafygreen-ui/typography";
import { agentApi } from "@/lib/api/client";
import ScenarioPicker from "./ScenarioPicker";
import ScenarioStepper from "./ScenarioStepper";
import styles from "./Showcase.module.css";

function useAgentHealth() {
  const [ready, setReady] = useState(null);
  useEffect(() => {
    let cancelled = false;
    agentApi("health").then(({ data, error }) => {
      if (!cancelled) setReady(!error && data?.reconciliationAgentReady === true);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return ready;
}

export default function ShowcaseView() {
  const [scenarioKey, setScenarioKey] = useState(null);
  const agentReady = useAgentHealth();

  // 16px base: the page is presented on a shared screen, and one base size keeps every
  // Body, Badge and Button on the same scale instead of per-class font sizes.
  return (
    <LeafyGreenProvider baseFontSize={16}>
      <div className={styles.root}>
        <div className={styles.header}>
          <div>
            <H2 className={styles.title}>Agent scenarios</H2>
            <Body className={styles.muted}>
              {scenarioKey
                ? "Click Next to run each step."
                : "Pick a scenario to watch an AI agent handle a payment problem, step by step."}
            </Body>
          </div>
          <span className={styles.health}>
            <span className={`${styles.dot} ${agentReady ? styles.dotUp : styles.dotDown}`} />
            <Body as="span">Agent {agentReady == null ? "checking…" : agentReady ? "ready" : "offline"}</Body>
          </span>
        </div>

        {agentReady === false && (
          <Banner variant="warning">
            The reconciliation agent is offline. Start payment_agent with
            ENABLE_RECONCILIATION_AGENT=true before running a scenario.
          </Banner>
        )}

        {scenarioKey ? (
          <ScenarioStepper key={scenarioKey} scenarioKey={scenarioKey} onReset={() => setScenarioKey(null)} />
        ) : (
          <ScenarioPicker onPick={setScenarioKey} />
        )}
      </div>
    </LeafyGreenProvider>
  );
}
