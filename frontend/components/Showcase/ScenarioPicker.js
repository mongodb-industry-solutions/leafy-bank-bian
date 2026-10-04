"use client";

import Card from "@leafygreen-ui/card";
import { H3, Body } from "@leafygreen-ui/typography";
import { SCENARIOS } from "./scenarios";
import styles from "./Showcase.module.css";

export default function ScenarioPicker({ onPick }) {
  return (
    <div className={styles.pickerGrid}>
      {SCENARIOS.map((s) => (
        <Card
          key={s.key}
          className={styles.scenarioCard}
          onClick={() => onPick(s.key)}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onPick(s.key)}
        >
          <div className={styles.cardHead}>
            <H3 className={styles.cardTitle}>{s.title}</H3>
          </div>
          <Body>{s.story}</Body>
          <Body className={styles.muted}>{s.talkingPoint}</Body>
          <div className={styles.cardFacts}>
            <Body className={styles.muted}>{s.bank}</Body>
            <Body className={styles.muted}>USD {s.amount}</Body>
            <Body>
              <strong>Outcome:</strong> {s.expected}
            </Body>
          </div>
        </Card>
      ))}
    </div>
  );
}
