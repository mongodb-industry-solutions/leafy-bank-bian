"use client";

import { useEffect, useState } from "react";
import Icon from "@leafygreen-ui/icon";
import { H2, H3, Body } from "@leafygreen-ui/typography";
import ScenarioInfo from "./ScenarioBrief";
import { ENABLED_SCENARIOS, GROUPS, PAIRS, stepsFor } from "./scenarios";
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

function ScenarioCard({ scenario, ran, onPick }) {
  const meta = scenario.startEt
    ? `${scenario.cardAmount || `USD ${scenario.amount}`} · ${scenario.startEt} ET`
    : scenario.bank
      ? `${scenario.bank} · USD ${scenario.amount} · ${stepsFor(scenario.key).length} steps`
      : scenario.cardAmount;
  return (
    <div className={`${styles.cardWrap} ${styles[`mode_${scenario.mode}`]}`}>
      <button type="button" className={styles.scenarioCard} onClick={() => onPick(scenario.key)}>
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
// all cards share one width; a pair fills its slot with two half-width cards under one heading.
const AGENTS = [
  { id: "recon", title: "Reconciliation agent", scenarios: ENABLED_SCENARIOS },
  { id: "cutoff", title: "Cut-off agent", scenarios: CUTOFF_SCENARIOS },
];

// A cell's scenarios as render items: a pair when all its keys sit in this cell, else single cards.
function cellItems(scenarios) {
  const items = [];
  const paired = new Set();
  for (const pair of PAIRS) {
    const members = pair.keys.map((k) => scenarios.find((s) => s.key === k));
    if (members.every(Boolean)) {
      items.push({ type: "pair", key: pair.keys.join("+"), pair, members });
      pair.keys.forEach((k) => paired.add(k));
    }
  }
  scenarios.filter((s) => !paired.has(s.key)).forEach((s) => items.push({ type: "card", key: s.key, scenario: s }));
  return items;
}

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
  const items = cellItems(agent.scenarios.filter((s) => s.mode === mode));
  return (
    <div className={styles.matrixCell}>
      <span className={styles.matrixCellAgent}>{agent.title}</span>
      {items.length === 0 && <div className={styles.matrixEmpty}>No scenario yet</div>}
      {items.map((item) => (item.type === "pair" ? (
        <div key={item.key} className={styles.pair}>
          <div className={styles.pairHead}>
            <span className={styles.pairTitle}>{item.pair.title}</span>
            <span className={styles.muted}>{item.pair.blurb}</span>
          </div>
          <div className={styles.pairCards}>
            {item.members.map((s) => (
              <ScenarioCard key={s.key} scenario={s} ran={ran.includes(s.key)} onPick={onPick} />
            ))}
          </div>
        </div>
      ) : (
        <ScenarioCard key={item.key} scenario={item.scenario} ran={ran.includes(item.key)} onPick={onPick} />
      )))}
    </div>
  );
}

function ScenarioMatrix({ ran, onPick }) {
  return (
    <div className={styles.matrix}>
      <div className={styles.matrixHeadRow}>
        <span />
        {AGENTS.map((a) => (
          <H2 key={a.id} className={styles.agentSectionTitle}>{a.title}</H2>
        ))}
      </div>
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
            <ScenarioCard key={s.key} scenario={s} ran={ran.includes(s.key)} onPick={onPick} />
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
