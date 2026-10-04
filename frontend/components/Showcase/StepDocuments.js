"use client";

// "Written to MongoDB": for each step already run, the collections it wrote and the live
// documents. One tab per completed step (the newest selected), so the presenter can go
// back to any earlier step. A step with a gate (Investigate, Approve) writes while it is
// on screen, so the current step gets a tab too.

import { useEffect, useState } from "react";
import Badge from "@leafygreen-ui/badge";
import Code from "@leafygreen-ui/code";
import { Tab, Tabs } from "@leafygreen-ui/tabs";
import { H3, Body } from "@leafygreen-ui/typography";
import { writesFor } from "./stepWrites";
import styles from "./Showcase.module.css";

const OP_VARIANT = { insert: "green", upsert: "green", update: "blue" };

function WriteRow({ write, sources }) {
  const docs = write.pick(sources);
  return (
    <details className={styles.write}>
      <summary className={styles.writeSummary}>
        <span className={styles.writeHead}>
          <Body as="span" className={styles.mono}>{write.collection}</Body>
          <Badge variant={OP_VARIANT[write.op] || "lightgray"}>{write.op}</Badge>
          {write.via && <Body as="span" className={styles.muted}>{write.via}</Body>}
        </span>
        <Body as="span">{write.fields}</Body>
      </summary>
      {docs ? (
        <Code language="json" copyButtonAppearance="hover" className={styles.writeCode}>
          {JSON.stringify(docs.length === 1 ? docs[0] : docs, null, 2)}
        </Code>
      ) : (
        <Body className={styles.muted}>
          {/* A zero-arity picker means no read route exposes this collection. */}
          {write.pick.length === 0
            ? "Not shown in this view."
            : "Not written yet. It appears here as soon as it lands."}
        </Body>
      )}
    </details>
  );
}

export default function StepDocuments({ steps, index, scenarioKey, sources }) {
  const current = steps[index];
  const lastTab = current?.gate || current?.final ? index : index - 1;
  const tabs = steps.slice(0, lastTab + 1).filter((s) => writesFor(s.key, scenarioKey).length);
  const [selected, setSelected] = useState(tabs.length - 1);

  // Jump to the newest step whenever one completes.
  useEffect(() => setSelected(tabs.length - 1), [tabs.length]);

  return (
    <div className={styles.pane}>
      <H3 className={styles.paneTitle}>Written to MongoDB</H3>
      {tabs.length === 0 ? (
        <Body className={styles.muted}>Each step&apos;s documents appear here once it runs.</Body>
      ) : (
        <Tabs aria-label="Documents written per step" selected={selected} setSelected={setSelected}>
          {tabs.map((s) => (
            <Tab key={s.key} name={s.label}>
              <div className={styles.writeList}>
                {writesFor(s.key, scenarioKey).map((w) => (
                  <WriteRow key={`${w.collection}-${w.op}`} write={w} sources={sources} />
                ))}
              </div>
            </Tab>
          ))}
        </Tabs>
      )}
    </div>
  );
}
