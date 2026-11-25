"use client";

import { useEffect, useState } from "react";
import Icon from "@leafygreen-ui/icon";
import { H3, Body } from "@leafygreen-ui/typography";
import ScenarioInfo from "./ScenarioBrief";
import { SCENARIOS, GROUPS, stepsFor } from "./scenarios";
import { CUTOFF_SCENARIOS } from "./cutoffScenarios";
import { CONCURRENCY_GROUP, CONCURRENCY_SCENARIOS } from "./concurrencyScenarios";
import styles from "./Showcase.module.css";

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
    <dl className={styles.decision}>
      {parts.map(([name, part]) => (
        <div key={name} className={styles.decisionRow} title={part.code}>
          <dt className={styles.decisionName}>{name}</dt>
          <dd className={`${styles.decisionLabel} ${name === "Action" ? styles.decisionAction : ""}`}>{part.label}</dd>
        </div>
      ))}
    </dl>
  );
}

function ScenarioCard({ scenario, ran, onPick, agent }) {
  const meta = scenario.startEt
    ? `${scenario.cardAmount || `USD ${scenario.amount}`} · ${scenario.startEt} ET`
    : scenario.bank
      ? `${scenario.bank} · USD ${scenario.amount} · ${stepsFor(scenario.key).length} steps`
      : scenario.cardAmount;
  return (
    <div className={`${styles.cardWrap} ${styles[`mode_${scenario.mode}`]}`}>
      <button type="button" className={styles.scenarioCard} onClick={() => onPick(scenario.key)}>
        <span className={`${styles.agentPill} ${styles[`agentPill_${agent.id}`]}`}>{agent.title}</span>
        <span className={styles.cardTitle}>{scenario.title}</span>
        <span className={styles.cardStory}>{scenario.story}</span>
        <DecisionFlow decision={scenario.decision} />
        <span className={styles.cardFooter}>
          <span className={styles.cardMeta}>{meta}</span>
          <span className={styles.cardBadges}>
            {ran && <span className={styles.ranMark}>Run</span>}
          </span>
        </span>
      </button>
      <ScenarioInfo scenario={scenario} className={styles.cardInfo} />
    </div>
  );
}

// One matrix: rows are what the agent does, columns are the agents. Every cell is one slot, so
// all cards share one width. Each card names its own agent in a pill (no column headers), so
// the no-agent concurrency row is not read as belonging to the first column's agent.
const NO_AGENT = { id: "none", title: "No agent · MongoDB" };
const AGENTS = [
  { id: "recon", title: "Reconciliation agent", scenarios: SCENARIOS },
  { id: "cutoff", title: "Cut-off agent", scenarios: CUTOFF_SCENARIOS },
];

function GroupHead({ group }) {
  return (
    <div className={styles.groupHead}>
      <span className={styles.groupIcon}>
        <Icon glyph={group.icon || MODE_ICON[group.mode]} size={14} />
      </span>
      <div>
        <H3 className={styles.groupTitle}>{group.title}</H3>
        <Body className={styles.muted}>{group.blurb}</Body>
      </div>
    </div>
  );
}

function Cell({ agent, mode, ran, onPick }) {
  const items = agent.scenarios.filter((s) => s.mode === mode);
  return (
    <div className={styles.matrixCell}>
      {items.length === 0 && <div className={styles.matrixEmpty}>No {agent.title.toLowerCase()} scenario yet</div>}
      {items.map((s) => (
        <ScenarioCard key={s.key} scenario={s} agent={agent} ran={ran.includes(s.key)} onPick={onPick} />
      ))}
    </div>
  );
}

function ScenarioMatrix({ ran, onPick }) {
  return (
    <div className={styles.matrix}>
      {GROUPS.map((group) => (
        <div key={group.mode} className={`${styles.matrixRow} ${styles[`mode_${group.mode}`]}`}>
          <GroupHead group={group} />
          {AGENTS.map((a) => (
            <Cell key={a.id} agent={a} mode={group.mode} ran={ran} onPick={onPick} />
          ))}
        </div>
      ))}
      <div className={`${styles.matrixRow} ${styles[`mode_${CONCURRENCY_GROUP.mode}`]}`}>
        <GroupHead group={CONCURRENCY_GROUP} />
        <div className={styles.matrixCell}>
          {CONCURRENCY_SCENARIOS.map((s) => (
            <ScenarioCard key={s.key} scenario={s} agent={NO_AGENT} ran={ran.includes(s.key)} onPick={onPick} />
          ))}
        </div>
      </div>
    </div>
  );
}

export default function ScenarioPicker({ onPick }) {
  const [ran, markRan] = useRanScenarios();
  const pick = (key) => {
    markRan(key);
    onPick(key);
  };

  return (
    <div id="showcase-scenarios" className={styles.picker}>
      <ScenarioMatrix ran={ran} onPick={pick} />
    </div>
  );
}
