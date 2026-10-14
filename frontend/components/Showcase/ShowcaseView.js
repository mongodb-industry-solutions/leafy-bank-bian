"use client";

// Plan E: the agent scenario walkthrough for the reconciliation and cut-off agents. Picker →
// stepper + narration → two panes. The step machines and their polls live in ScenarioStepper
// (reconciliation) and CutoffStepper (cut-off).

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Banner from "@leafygreen-ui/banner";
import LeafyGreenProvider from "@leafygreen-ui/leafygreen-provider";
import { Body } from "@leafygreen-ui/typography";
import { agentApi } from "@/lib/api/client";
import { scenarioByKey } from "./scenarios";
import ScenarioPicker from "./ScenarioPicker";
import ScenarioStepper from "./ScenarioStepper";
import { cutoffByKey } from "./cutoffScenarios";
import CutoffStepper from "./CutoffStepper";
import styles from "./Showcase.module.css";

const AGENT_COPY = {
  reconciliation: {
    name: "Reconciliation",
    offline: "The reconciliation agent is offline. Start payment_agent with ENABLE_RECONCILIATION_AGENT=true before running a scenario.",
  },
  cutoff: {
    name: "Cut-off",
    offline: "The cut-off agent is offline. Start payment_agent with ENABLE_CUTOFF_AGENT=true before running a scenario.",
  },
};

/** Readiness per agent: { reconciliation, cutoff }, each null while checking. */
function useAgentHealth() {
  const [ready, setReady] = useState({ reconciliation: null, cutoff: null });
  useEffect(() => {
    let cancelled = false;
    agentApi("health").then(({ data, error }) => {
      if (cancelled) return;
      setReady({
        reconciliation: !error && data?.reconciliationAgentReady === true,
        cutoff: !error && data?.cutoffAgentReady === true && data?.cutoffAgentEnabled !== false,
      });
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return ready;
}

export default function ShowcaseView() {
  // The chosen scenario lives in the URL so browser Back and the Showcase nav tab both
  // return to the picker.
  const router = useRouter();
  const params = useSearchParams();
  const param = params.get("scenario");
  const isCutoffKey = !!cutoffByKey(param);
  const scenarioKey = isCutoffKey || scenarioByKey(param) ? param : null;
  const setScenarioKey = (key) =>
    router.push(key ? `/showcase?scenario=${encodeURIComponent(key)}` : "/showcase");
  const health = useAgentHealth();
  const states = Object.values(health);
  const checking = states.some((s) => s == null);
  const allLive = states.every((s) => s === true);
  const statusDot = checking ? "" : allLive ? styles.dotUp : styles.dotDown;

  // 16px base: the page is presented on a shared screen, and one base size keeps every
  // Body, Badge and Button on the same scale instead of per-class font sizes.
  return (
    <LeafyGreenProvider baseFontSize={16}>
      <div className={styles.page}>
        <div className={styles.root}>
          {!scenarioKey && <div className={styles.hero}>
            <span className={styles.health} title={Object.entries(AGENT_COPY).map(([key, c]) => `${c.name}: ${health[key] ? "ready" : "offline"}`).join(" · ")}>
              <span className={`${styles.dot} ${statusDot}`} />
              <Body as="span">Agents</Body>
            </span>
          </div>}

        {!scenarioKey && Object.entries(AGENT_COPY).map(([key, c]) =>
          health[key] === false && <Banner key={key} variant="warning">{c.offline}</Banner>)}

        {scenarioKey && isCutoffKey && (
          <CutoffStepper key={scenarioKey} scenarioKey={scenarioKey} onReset={() => setScenarioKey(null)} />
        )}
        {scenarioKey && !isCutoffKey && (
          <ScenarioStepper key={scenarioKey} scenarioKey={scenarioKey} onReset={() => setScenarioKey(null)} />
        )}
        {!scenarioKey && <ScenarioPicker onPick={setScenarioKey} />}
        </div>
      </div>
    </LeafyGreenProvider>
  );
}
