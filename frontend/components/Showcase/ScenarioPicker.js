"use client";

import { useEffect, useState } from "react";
import Icon from "@leafygreen-ui/icon";
import { H2, H3, Body } from "@leafygreen-ui/typography";
import ScenarioInfo from "./ScenarioBrief";
import { ENABLED_SCENARIOS, GROUPS, stepsFor } from "./scenarios";
import { CUTOFF_SCENARIOS, CUTOFF_GROUPS } from "./cutoffScenarios";
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
    : `${scenario.bank} · USD ${scenario.amount} · ${stepsFor(scenario.key).length} steps`;
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

// Every card is one column of the same four-column grid in both sections, so all cards share one size.
// A group spans as many columns as it has cards.
const CARD_COLUMNS = 4;

function AgentSection({ id, title, groups, scenarios, ran, onPick }) {
  const filled = groups
    .map((group) => ({ group, items: scenarios.filter((s) => s.mode === group.mode) }))
    .filter(({ items }) => items.length);
  return (
    <section className={styles.agentSection} aria-labelledby={id}>
      <H2 id={id} className={styles.agentSectionTitle}>{title}</H2>
      <div className={styles.groupRow} style={{ "--cols": CARD_COLUMNS }}>
        {filled.map(({ group, items }) => (
          <div key={group.mode} className={`${styles.group} ${styles[`mode_${group.mode}`]}`}
            style={{ "--span": items.length }}>
            <div className={styles.groupHead}>
              <span className={styles.groupIcon}>
                <Icon glyph={group.icon || MODE_ICON[group.mode]} size={14} />
              </span>
              <div>
                <H3 className={styles.groupTitle}>{group.title}</H3>
                <Body className={styles.muted}>{group.blurb}</Body>
              </div>
            </div>
            <div className={styles.pickerGrid} style={{ "--cols": items.length }}>
              {items.map((s) => (
                <ScenarioCard key={s.key} scenario={s} ran={ran.includes(s.key)} onPick={onPick} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

// Both agents on one page, reconciliation first.
export default function ScenarioPicker({ onPick }) {
  const [ran, markRan] = useRanScenarios();
  const pick = (key) => {
    markRan(key);
    onPick(key);
  };

  return (
    <div id="showcase-scenarios" className={styles.picker}>
      <AgentSection id="recon-scenarios" title="Reconciliation agent" groups={GROUPS}
        scenarios={ENABLED_SCENARIOS} ran={ran} onPick={pick} />
      <AgentSection id="cutoff-scenarios" title="Cut-off agent" groups={CUTOFF_GROUPS}
        scenarios={CUTOFF_SCENARIOS} ran={ran} onPick={pick} />
    </div>
  );
}
