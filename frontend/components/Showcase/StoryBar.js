"use client";

// The one progress model on the page: the acts of the story. The Problem node follows the
// page tone, so an open exception is visible from anywhere in the story.

import Icon from "@leafygreen-ui/icon";
import { Body } from "@leafygreen-ui/typography";
import styles from "./Showcase.module.css";

function nodeState(act, currentAct) {
  if (act.id < currentAct) return "done";
  if (act.id === currentAct) return "current";
  return "ahead";
}

export default function StoryBar({ acts, currentAct, tone }) {
  return (
    <ol className={styles.storyBar} aria-label="Story progress">
      {acts.map((act) => {
        const state = nodeState(act, currentAct);
        // Problem node carries the exception's colour once the story has reached it.
        const toned = act.id === 2 && state !== "ahead" && tone !== "neutral" ? tone : "";
        return (
          <li
            key={act.id}
            className={`${styles.storyNode} ${styles[`story_${state}`]} ${toned ? styles[`tone_${toned}`] : ""}`}
            aria-current={state === "current" ? "step" : undefined}
          >
            <span className={styles.storyDot}>
              {state === "done" ? <Icon glyph="Checkmark" size={14} /> : act.id}
            </span>
            <Body as="span" className={styles.storyLabel}>{act.label}</Body>
          </li>
        );
      })}
    </ol>
  );
}
