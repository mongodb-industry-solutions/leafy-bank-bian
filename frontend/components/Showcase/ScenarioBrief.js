"use client";

// A small "i" that reveals the full scenario brief on hover or keyboard focus: the
// situation, why it is hard, what the agent decides, what code guarantees, what the agent
// does alone, what needs a human, the MongoDB features, and the autonomy level.
// It sits beside the card, not inside it, because a card in the picker is itself a button.

import Badge from "@leafygreen-ui/badge";
import Icon from "@leafygreen-ui/icon";
import { Body, Overline } from "@leafygreen-ui/typography";
import { AUTONOMY_LEVELS, briefFor } from "./briefs";
import styles from "./Showcase.module.css";

function AutonomyDial({ level }) {
  const active = AUTONOMY_LEVELS[level];
  return (
    <div className={styles.dial} role="img" aria-label={`Autonomy: ${active.label}. ${active.hint}.`}>
      <div className={styles.dialTrack}>
        {AUTONOMY_LEVELS.map((l, i) => (
          <span key={l.label} className={`${styles.dialStop} ${i === level ? styles.dialStopOn : ""}`} />
        ))}
      </div>
      <Body as="span" className={styles.dialLabel}>{active.label}</Body>
    </div>
  );
}

const Fact = ({ title, children }) => (
  <div className={styles.briefFact}>
    <Overline>{title}</Overline>
    <Body>{children}</Body>
  </div>
);

export default function ScenarioInfo({ scenario, className = "" }) {
  const brief = briefFor(scenario.key);
  if (!brief) return null;
  return (
    <span className={`${styles.infoWrap} ${className}`}>
      <button type="button" className={styles.infoBtn} aria-label={`About: ${scenario.title}`}>
        <Icon glyph="InfoWithCircle" size={14} />
      </button>
      <span className={styles.infoPop} role="tooltip">
        <span className={styles.infoPopInner}>
          <AutonomyDial level={brief.autonomy.level} />
          <Fact title="Why it is hard">{brief.hard}</Fact>
          <Fact title="The agent decides">{brief.decides}</Fact>
          <Fact title="Code guarantees">{brief.guarantees}</Fact>
          <Fact title="Agent does alone">{brief.autonomy.alone}</Fact>
          <Fact title="Needs you">{brief.autonomy.needsYou}</Fact>
          <span className={styles.briefFact}>
            <Overline>MongoDB features</Overline>
            <span className={styles.chipRow}>
              {brief.mongo.map((m) => (
                <Badge key={m} variant="green">{m}</Badge>
              ))}
            </span>
          </span>
        </span>
      </span>
    </span>
  );
}
