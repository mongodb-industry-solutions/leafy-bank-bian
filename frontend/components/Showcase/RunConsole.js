"use client";

// The central panel of both agent walkthroughs. It starts every step (the Next bar),
// shows the run as one log of events, and puts the human decision in the AI panel beside it. There are
// no acts: progress is the log.

import { useEffect, useRef, useState } from "react";
import Badge from "@leafygreen-ui/badge";
import Button from "@leafygreen-ui/button";
import Icon from "@leafygreen-ui/icon";
import { Body, Overline } from "@leafygreen-ui/typography";
import { WriteRow } from "./StepDocuments";
import { LANE_LABEL, PHASES } from "./runLog";
import styles from "./Showcase.module.css";

const FILTERS = [["all", "All"], ["agent", "Agent"], ["code", "Code"], ["mongo", "MongoDB"], ["human", "You"]];
const LANE_ICON = { agent: "Sparkle", code: "CurlyBraces", mongo: "Database", human: "Person" };
const time = (at) => new Date(at).toLocaleTimeString([], { hour12: false });
const NEAR_BOTTOM_PX = 32;

// Milestones (cards, toned events) stand alone. Everything else is routine, so consecutive
// routine events from one lane share a node and a header instead of repeating them.
const isMilestone = (e) => !!e.card || !!e.tone;

// Splits the shown events into blocks: a phase divider (when the phase changes) plus one or
// more events that render under a single lane node.
function toBlocks(shown) {
  const blocks = [];
  let lastPhase = null;
  for (const e of shown) {
    const divider = e.phase !== lastPhase && PHASES.includes(e.phase) ? e.phase : null;
    lastPhase = e.phase;
    const prev = blocks[blocks.length - 1];
    const joins = prev && !divider && !isMilestone(e) && !isMilestone(prev.events[0]) && prev.events[0].lane === e.lane;
    if (joins) prev.events.push(e);
    else blocks.push({ key: e.key, divider, events: [e] });
  }
  return blocks;
}

// Findings and policy checks are the point of the run, so they show their content up front
// instead of hiding it behind a click.
function FeatureBody({ event }) {
  const tag = event.tag && <Badge variant="green" className={styles.runTag}>{event.tag}</Badge>;
  return (
    <>
      <Body as="div" className={styles.runTitle}>{event.title}{tag}</Body>
      {event.detail && <Body as="div" className={styles.muted}>{event.detail}</Body>}
      {event.stall && <div className={styles.stallBar} aria-hidden="true" />}
      {event.items?.length > 0 && (
        <ul className={styles.featureList}>
          {event.items.map((it) => (
            <li key={it}><Icon glyph="Checkmark" size={12} className={styles.featureTick} />{it}</li>
          ))}
        </ul>
      )}
      {event.checks?.length > 0 && (
        <ul className={styles.featureList}>
          {event.checks.map((c) => (
            <li key={c.text} className={c.ok ? styles.checkOk : styles.checkBad}>
              <Icon glyph={c.ok ? "CheckmarkWithCircle" : "XWithCircle"} size={14} />
              <span><strong>{c.ok ? "Passed" : "Refused"}</strong> {c.text}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function EventBody({ event, sources }) {
  if (event.card) return <FeatureBody event={event} />;
  const thought = event.thought && <Body as="div" className={styles.thought}>“{event.thought}”</Body>;
  const detail = event.detail && <Body as="div" className={`${styles.muted} ${styles.mono}`}>{event.detail}</Body>;
  const tag = event.tag && <Badge variant="green" className={styles.runTag}>{event.tag}</Badge>;
  const expandable = event.expand || event.items?.length || event.writes?.length;
  if (!expandable) {
    return (
      <>
        <Body as="div" className={styles.runTitle}>{event.title}{tag}</Body>
        {thought}
        {detail}
      </>
    );
  }
  return (
    <details>
      <summary className={styles.runTitle}>{event.title}{tag}</summary>
      {thought}
      {detail}
      {event.expand && <pre className={styles.tlPre}>{event.expand}</pre>}
      {event.items?.length > 0 && (
        <Body as="ul" className={styles.evidence}>
          {event.items.map((it) => <li key={it}>{it}</li>)}
        </Body>
      )}
      {event.writes && (
        <div className={styles.writeList}>
          {event.writes.map((w) => (
            <WriteRow key={`${w.collection}-${w.op}`} write={w} sources={sources} />
          ))}
        </div>
      )}
    </details>
  );
}

// One card per step: a short summary of what is happening, plus what it means for this
// payment. The arrows browse steps already seen (and the ones ahead) without running anything;
// the card follows the run again as soon as the next step starts.
function StepCards({ cards, current, final, waiting }) {
  const [viewed, setViewed] = useState(current);
  useEffect(() => setViewed(current), [current]);
  const card = cards[viewed];
  const isCurrent = viewed === current;
  return (
    <div className={styles.stepCard}>
      <div className={styles.stepCardNav}>
        <button type="button" className={styles.stepArrow} aria-label="Previous step" disabled={viewed === 0} onClick={() => setViewed(viewed - 1)}>
          <Icon glyph="ChevronLeft" size={16} />
        </button>
        <Overline>
          {isCurrent ? (final ? "Run" : "Next") : viewed < current ? "Done" : "Coming up"} · Step {viewed + 1} of {cards.length}
        </Overline>
        <button type="button" className={styles.stepArrow} aria-label="Next step" disabled={viewed === cards.length - 1} onClick={() => setViewed(viewed + 1)}>
          <Icon glyph="ChevronRight" size={16} />
        </button>
        {!isCurrent && (
          <button type="button" className={styles.stepBack} onClick={() => setViewed(current)}>Back to current</button>
        )}
      </div>
      <Body className={styles.runNext}>{card.title}</Body>
      <div className={styles.stepCols}>
        <div className={styles.stepCol}>
          <Overline>What is happening</Overline>
          <Body>{card.summary}</Body>
        </div>
        {card.beat && (
          <div className={`${styles.stepCol} ${styles.stepBeat}`}>
            <Overline>In this scenario</Overline>
            <Body>{card.beat}</Body>
          </div>
        )}
      </div>
      {isCurrent && waiting && <Body className={styles.muted}>{waiting}</Body>}
    </div>
  );
}

const SLOW_AFTER_S = 20;
const ACTIVITY_ICON = { agent: "Sparkle", code: "CurlyBraces", human: "Person", external: "Clock" };

// One line saying what the run is doing or waiting on. The timer restarts when the state
// changes; after SLOW_AFTER_S it reassures the audience the app is still working.
function ActivityBanner({ activity }) {
  const key = activity ? `${activity.kind}:${activity.text}` : null;
  const [since, setSince] = useState(Date.now());
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    setSince(Date.now());
    setNow(Date.now());
  }, [key]);
  useEffect(() => {
    if (!key) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [key]);
  if (!activity) return null;
  const seconds = Math.max(0, Math.floor((now - since) / 1000));
  const waitsOnYou = activity.kind === "human";
  return (
    <div className={`${styles.activity} ${styles[`lane_${activity.kind === "external" ? "code" : activity.kind}`]}`} role="status" aria-live="polite">
      <span className={styles.activityIcon} aria-hidden="true">
        <Icon glyph={ACTIVITY_ICON[activity.kind]} size={14} />
      </span>
      <span className={styles.activityText}>{activity.text}{waitsOnYou ? "" : "…"}</span>
      {!waitsOnYou && <span className={styles.activityTime}>{seconds}s</span>}
      {!waitsOnYou && seconds >= SLOW_AFTER_S && (
        <span className={styles.activityHint}>Still working. This can take up to a minute.</span>
      )}
    </div>
  );
}

export default function RunConsole({
  top, pipeline, stepCards, stepIndex, controller, events, pending, sources, evidence, aside, failure, result, activity, decision,
}) {
  const [filter, setFilter] = useState("all");
  const logRef = useRef(null);
  const following = useRef(true);
  const lastCount = useRef(0);
  const [unseen, setUnseen] = useState(0);
  const shown = filter === "all" ? events : events.filter((e) => e.lane === filter);

  const scrollToLatest = () => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    following.current = true;
    setUnseen(0);
  };

  // Follow the log only while the viewer is at the bottom; scrolling up pauses it and new
  // events are counted instead.
  const onLogScroll = () => {
    const el = logRef.current;
    if (!el) return;
    following.current = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;
    if (following.current) setUnseen(0);
  };

  useEffect(() => {
    const added = Math.max(0, shown.length - lastCount.current);
    lastCount.current = shown.length;
    if (following.current) scrollToLatest();
    else if (added) setUnseen((n) => n + added);
  }, [shown.length, pending]);

  useEffect(() => {
    lastCount.current = shown.length;
    scrollToLatest();
  }, [filter]);

  const actions = (
      <div className={styles.runActions}>
        {controller.showButton && (
          <Button variant="primary" size="large" disabled={controller.running || !!controller.waiting} onClick={controller.onNext}>
            {controller.running ? "Running…" : controller.label}
          </Button>
        )}
        {controller.awaitingReply && (
          <Button size="small" disabled={controller.running} onClick={controller.onReplyNow}>
            Correspondent replies now
          </Button>
        )}
        {controller.onSweep && (
          <Button size="small" disabled={controller.sweeping || controller.sweepDisabled} onClick={controller.onSweep}>
            {controller.sweeping ? "Agent running…" : "Run now"}
          </Button>
        )}
      </div>
  );
  const blocks = toBlocks(shown);
  return (
    <div className={aside ? styles.consoleGrid : undefined}>
    <section className={`${styles.scene} ${styles.toneStripe}`}>
      {top && <div className={styles.runTop}>{top}</div>}
      {pipeline}

      <div className={styles.runRow}>
        <div className={styles.runControl}>
          <StepCards cards={stepCards} current={stepIndex} final={controller.final} waiting={controller.waiting} />
        </div>
      </div>

      {controller.error && <Body className={styles.error}>{controller.error}</Body>}
      <ActivityBanner activity={activity} />
      {evidence}
      {failure}

      <div className={styles.runLogBox}>
      <div className={styles.runLogHead}>
        <span className={styles.runLogTitle}>
          <Overline>Run log</Overline>
          <span className={styles.liveDot} aria-hidden="true" />
        </span>
        <div className={styles.runFilters} role="group" aria-label="Filter the log">
          {FILTERS.map(([value, text]) => (
            <button
              key={value}
              type="button"
              className={`${styles.runFilter} ${value !== "all" ? styles[`lane_${value}`] : ""} ${filter === value ? styles.runFilterOn : ""}`}
              aria-pressed={filter === value}
              onClick={() => setFilter(value)}
            >
              {value !== "all" && <Icon glyph={LANE_ICON[value]} size={12} />}
              {text}
              <span className={styles.runCount}>
                {value === "all" ? events.length : events.filter((e) => e.lane === value).length}
              </span>
            </button>
          ))}
        </div>
      </div>

      <div className={styles.runLog} ref={logRef} onScroll={onLogScroll} aria-live="polite">
        {shown.length === 0 && (
          <Body className={styles.muted}>
            {events.length === 0 ? "Events appear here as the run progresses." : "No events in this filter yet."}
          </Body>
        )}
        {blocks.map((b) => {
          const first = b.events[0];
          const many = b.events.length > 1;
          return (
            <div key={b.key}>
              {b.divider && (
                <div className={styles.runPhase}>
                  <span className={styles.runPhaseChip}>{b.divider}</span>
                  <span className={styles.runPhaseRule} />
                </div>
              )}
              <div className={`${styles.runEvent} ${styles[`lane_${first.lane}`]}`}>
                <span className={styles.runNode} aria-hidden="true">
                  <Icon glyph={LANE_ICON[first.lane]} size={14} />
                </span>
                <div className={`${styles.runCard} ${first.card ? styles.runCardFeature : ""} ${first.tone ? styles[`runCard_${first.tone}`] : ""} ${!isMilestone(first) ? styles.runCardQuiet : ""}`}>
                  <div className={styles.runMeta}>
                    <span className={styles.runLane}>{LANE_LABEL[first.lane]}{many ? ` · ${b.events.length}` : ""}</span>
                    {!many && Number.isFinite(first.at) && <span className={styles.muted}>{time(first.at)}</span>}
                  </div>
                  {b.events.map((e) => (
                    <div key={e.key} className={many ? styles.logRow : undefined}>
                      <div className={styles.logRowBody}><EventBody event={e} sources={sources} /></div>
                      {many && Number.isFinite(e.at) && <span className={`${styles.muted} ${styles.logRowTime}`}>{time(e.at)}</span>}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          );
        })}
        {pending && (
          <div className={styles.runPending} role="status">
            <span className={styles.liveDot} aria-hidden="true" />
            {pending}
          </div>
        )}
      </div>
      {unseen > 0 && (
        <button type="button" className={styles.jumpLatest} onClick={scrollToLatest}>
          <Icon glyph="ArrowDown" size={12} />
          Jump to latest ({unseen} new)
        </button>
      )}
      </div>

      {result}

      {/* Docked to the bottom of the viewport while the proposal waits, so it is never off screen. */}
      {decision && (
        <div className={styles.decisionDock} role="region" aria-label="Needs your decision">
          <Overline>Needs your decision</Overline>
          {decision}
        </div>
      )}
    </section>
    <div className={styles.rightCol}>
      {actions}
      {aside}
    </div>
    </div>
  );
}
