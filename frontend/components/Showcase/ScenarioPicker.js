"use client";

import { useEffect, useState } from "react";
import Icon from "@leafygreen-ui/icon";
import { SegmentedControl, SegmentedControlOption } from "@leafygreen-ui/segmented-control";
import { H3, Body } from "@leafygreen-ui/typography";
import { SCENARIOS, GROUPS, stepsFor } from "./scenarios";
import { CUTOFF_SCENARIOS, CUTOFF_GROUPS } from "./cutoffScenarios";
import styles from "./Showcase.module.css";

const MODE_LABEL = {
  approval: "Needs approval",
  refuses: "Refuses to guess",
  autonomous: "No approval needed",
};

const MODE_ICON = {
  approval: "Person",
  refuses: "Stop",
  autonomous: "Refresh",
};

const RAN_KEY = "showcase.ran";

function useRanScenarios() {
  const [ran, setRan] = useState([]);
  useEffect(() => {
    try {
      setRan(JSON.parse(sessionStorage.getItem(RAN_KEY) || "[]"));
    } catch {
      setRan([]);
    }
  }, []);
  const markRan = (key) => {
    const next = [...new Set([...ran, key])];
    setRan(next);
    sessionStorage.setItem(RAN_KEY, JSON.stringify(next));
  };
  return [ran, markRan];
}

function DecisionFlow({ decision }) {
  const parts = [
    ["Cause", decision.cause],
    ["Rule", decision.constraint],
    ["Action", decision.action],
  ];
  return (
    <ol className={styles.decision}>
      {parts.map(([name, part]) => (
        <li key={name} className={styles.decisionRow} title={part.code}>
          <span className={styles.decisionName}>{name}</span>
          <span className={styles.decisionLabel}>{part.label}</span>
        </li>
      ))}
    </ol>
  );
}

function ScenarioCard({ scenario, ran, onPick }) {
  return (
    <button
      type="button"
      className={`${styles.scenarioCard} ${styles[`mode_${scenario.mode}`]}`}
      onClick={() => onPick(scenario.key)}
    >
      <div className={styles.cardTop}>
        <span className={styles.modeIcon}>
          <Icon glyph={scenario.icon || MODE_ICON[scenario.mode]} size={16} />
        </span>
        <span className={styles.modeLabel}>{scenario.modeLabel || MODE_LABEL[scenario.mode]}</span>
        <span className={styles.cardBadges}>
          {scenario.recommended && <span className={styles.suggested}>Suggested</span>}
          {ran && <span className={styles.ranMark}>Run</span>}
        </span>
      </div>
      <H3 className={styles.cardTitle}>{scenario.title}</H3>
      <Body className={styles.cardStory}>{scenario.story}</Body>
      <DecisionFlow decision={scenario.decision} />
      <div className={styles.cardFooter}>
        <span className={styles.cardMeta}>
          {scenario.startEt
            ? `USD ${scenario.amount} · starts ${scenario.startEt} ET`
            : `${scenario.bank} · USD ${scenario.amount} · ${stepsFor(scenario.key).length} steps`}
        </span>
        <span className={styles.startCue}>
          Start <Icon glyph="ArrowRight" size={14} />
        </span>
      </div>
    </button>
  );
}

export default function ScenarioPicker({ agent, onAgentChange, onPick }) {
  const [ran, markRan] = useRanScenarios();
  const pick = (key) => {
    markRan(key);
    onPick(key);
  };
  const cutoff = agent === "cutoff";
  const groups = cutoff ? CUTOFF_GROUPS : GROUPS;
  const scenarios = cutoff ? CUTOFF_SCENARIOS : SCENARIOS;

  return (
    <div id="showcase-scenarios" className={styles.picker}>
      <div className={styles.agentToggle}>
        <SegmentedControl aria-label="Agent" aria-controls="showcase-scenarios" value={cutoff ? "cutoff" : "reconciliation"} onChange={onAgentChange}>
          <SegmentedControlOption value="reconciliation">Reconciliation</SegmentedControlOption>
          <SegmentedControlOption value="cutoff">Cut-off</SegmentedControlOption>
        </SegmentedControl>
      </div>
      {groups.map((group) => (
        <section key={group.mode} className={`${styles.group} ${styles[`mode_${group.mode}`]}`}>
          <div className={styles.groupHead}>
            <span className={styles.groupIcon}>
              <Icon glyph={group.icon || MODE_ICON[group.mode]} size={16} />
            </span>
            <div>
              <H3 className={styles.groupTitle}>{group.title}</H3>
              <Body className={styles.muted}>{group.blurb}</Body>
            </div>
          </div>
          <div className={styles.pickerGrid}>
            {scenarios.filter((s) => s.mode === group.mode).map((s) => (
              <ScenarioCard key={s.key} scenario={s} ran={ran.includes(s.key)} onPick={pick} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
