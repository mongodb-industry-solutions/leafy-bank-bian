"use client";

// Plan E: the reconciliation scenario walkthrough. Picker → stepper + narration → two panes
// (payment tracker, agent thinking). The step machine and its polls live in ScenarioStepper.

import { useEffect, useState } from "react";
import Banner from "@leafygreen-ui/banner";
import { H1, Body } from "@leafygreen-ui/typography";
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

  return (
    <div className={styles.root}>
      <div className={styles.header}>
        <H1 className={styles.title}>Reconciliation walkthrough</H1>
        <span className={styles.health}>
          <span className={`${styles.dot} ${agentReady ? styles.dotUp : styles.dotDown}`} />
          Agent {agentReady == null ? "…" : agentReady ? "ready" : "down"}
        </span>
      </div>

      {agentReady === false && (
        <Banner variant="warning" className={styles.banner}>
          The reconciliation agent is not reachable. Start payment_agent with
          ENABLE_RECONCILIATION_AGENT=true before running a scenario.
        </Banner>
      )}
      <Banner variant="info" className={styles.banner}>
        Presenter precondition: run the transactions service with ENABLE_STATEMENT_SIM=false,
        otherwise the background simulator books statements out of order.
      </Banner>

      {scenarioKey ? (
        <ScenarioStepper key={scenarioKey} scenarioKey={scenarioKey} onReset={() => setScenarioKey(null)} />
      ) : (
        <>
          <Body className={styles.lead}>Pick a scenario. Each Next click runs one step.</Body>
          <ScenarioPicker onPick={setScenarioKey} />
        </>
      )}
    </div>
  );
}
