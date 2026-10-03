"use client";

import Card from "@leafygreen-ui/card";
import Badge from "@leafygreen-ui/badge";
import { H3, Body, Disclaimer } from "@leafygreen-ui/typography";
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
            <Badge variant="blue">{s.key}</Badge>
            <H3 className={styles.cardTitle}>{s.title}</H3>
          </div>
          <Body className={styles.cardStory}>{s.story}</Body>
          <Body className={styles.cardMeta}>
            {[s.bank, s.amount].filter(Boolean).join(" · ")}
          </Body>
          <Body className={styles.cardExpected}>
            <strong>Expected:</strong> {s.expected}
          </Body>
          <Disclaimer className={styles.muted}>{s.talkingPoint}</Disclaimer>
        </Card>
      ))}
    </div>
  );
}
