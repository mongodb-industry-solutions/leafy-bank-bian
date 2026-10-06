"use client";

// Plan E: the agent scenario walkthrough for the reconciliation and cut-off agents. Picker →
// stepper + narration → two panes. The step machines and their polls live in ScenarioStepper
// (reconciliation) and CutoffStepper (cut-off).

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Banner from "@leafygreen-ui/banner";
import Icon from "@leafygreen-ui/icon";
import LeafyGreenProvider from "@leafygreen-ui/leafygreen-provider";
import { H2, Body } from "@leafygreen-ui/typography";
import { agentApi } from "@/lib/api/client";
import { scenarioByKey } from "./scenarios";
import ScenarioPicker from "./ScenarioPicker";
import ScenarioStepper from "./ScenarioStepper";
import { cutoffByKey } from "./cutoffScenarios";
import CutoffStepper from "./CutoffStepper";
import styles from "./Showcase.module.css";

const AGENT_COPY = {
  reconciliation: {
    eyebrow: "Reconciliation agent",
    blurb:
      "Watch an AI agent investigate a payment problem, propose a fix, and wait for a human to approve. Cards marked Suggested make a good short demo.",
    offline: "The reconciliation agent is offline. Start payment_agent with ENABLE_RECONCILIATION_AGENT=true before running a scenario.",
  },
  cutoff: {
    eyebrow: "Cut-off agent",
    blurb:
      "Watch an AI agent race a held wire against the day's cut-off: it clears what it can on its own, and asks a human before it expedites, holds or defers. Cards marked Suggested make a good short demo.",
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
        cutoff: !error && data?.cutoffAgentReady === true,
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
  const agent = isCutoffKey || params.get("agent") === "cutoff" ? "cutoff" : "reconciliation";
  const base = agent === "cutoff" ? "/showcase?agent=cutoff" : "/showcase";
  const setScenarioKey = (key) =>
    router.push(key ? `${base}${agent === "cutoff" ? "&" : "?"}scenario=${encodeURIComponent(key)}` : base);
  const setAgent = (next) => router.push(next === "cutoff" ? "/showcase?agent=cutoff" : "/showcase");
  const agentReady = useAgentHealth()[agent];
  const copy = AGENT_COPY[agent];

  // 16px base: the page is presented on a shared screen, and one base size keeps every
  // Body, Badge and Button on the same scale instead of per-class font sizes.
  return (
    <LeafyGreenProvider baseFontSize={16}>
      <div className={styles.page}>
        <div className={styles.root}>
          <div className={scenarioKey ? styles.header : styles.hero}>
            <div className={styles.heroText}>
              {!scenarioKey && (
                <span className={styles.eyebrow}>
                  <Icon glyph="Sparkle" size={14} /> {copy.eyebrow}
                </span>
              )}
              <H2 className={styles.title}>Agent scenarios</H2>
              <Body className={styles.muted}>
                {scenarioKey
                  ? "Click Next to run each step."
                  : copy.blurb}
              </Body>
            </div>
            <span className={styles.health}>
              <span className={`${styles.dot} ${agentReady ? styles.dotUp : styles.dotDown}`} />
              <Body as="span">Agent {agentReady == null ? "checking…" : agentReady ? "ready" : "offline"}</Body>
            </span>
          </div>

        {agentReady === false && (
          <Banner variant="warning">{copy.offline}</Banner>
        )}

        {scenarioKey && isCutoffKey && (
          <CutoffStepper key={scenarioKey} scenarioKey={scenarioKey} onReset={() => setScenarioKey(null)} />
        )}
        {scenarioKey && !isCutoffKey && (
          <ScenarioStepper key={scenarioKey} scenarioKey={scenarioKey} onReset={() => setScenarioKey(null)} />
        )}
        {!scenarioKey && <ScenarioPicker agent={agent} onAgentChange={setAgent} onPick={setScenarioKey} />}
        </div>
      </div>
    </LeafyGreenProvider>
  );
}
