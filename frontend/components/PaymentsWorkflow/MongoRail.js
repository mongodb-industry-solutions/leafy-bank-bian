"use client";

// The MongoDB rail beside a lifecycle stage: why MongoDB fits this stage, then one row per
// collection the stage writes (operation badge, what changed, live document). The first row
// with a document opens expanded. In the Business lens the rail collapses to a one-line
// summary the presenter can expand.

import { useState } from "react";
import Badge from "@leafygreen-ui/badge";
import Code from "@leafygreen-ui/code";
import { Body } from "@leafygreen-ui/typography";
import { writeSummary } from "./stageContent";
import styles from "./MongoRail.module.css";

const OP_VARIANT = { insert: "green", upsert: "green", update: "blue" };

function WriteRow({ write, sources, open }) {
  const docs = write.pick(sources);
  return (
    <details className={styles.row} open={open}>
      <summary className={styles.summary}>
        <span className={styles.head}>
          <span className={styles.collection}>{write.collection}</span>
          {!write.notApplicable && (
            <Badge variant={OP_VARIANT[write.op] || "lightgray"}>{write.op}</Badge>
          )}
        </span>
        <span className={styles.fields}>{write.fields}</span>
        {write.via && <span className={styles.via}>{write.via}</span>}
      </summary>
      {write.notApplicable ? null : docs ? (
        <div className={styles.code}>
          <Code language="json" copyButtonAppearance="hover">
            {JSON.stringify(docs.length === 1 ? docs[0] : docs, null, 2)}
          </Code>
        </div>
      ) : (
        <Body className={styles.empty}>Not written yet. It appears here as soon as it lands.</Body>
      )}
    </details>
  );
}

export default function MongoRail({ writes, why, sources, collapsed }) {
  const [expanded, setExpanded] = useState(false);
  if (!writes.length) return null;

  if (collapsed && !expanded) {
    const { documents, collections } = writeSummary(writes, sources);
    return (
      <div className={styles.strip}>
        <span>
          {documents} document{documents === 1 ? "" : "s"} written in {collections}{" "}
          collection{collections === 1 ? "" : "s"}
        </span>
        <button type="button" className={styles.link} onClick={() => setExpanded(true)}>
          Show MongoDB detail
        </button>
      </div>
    );
  }

  const firstWithDoc = writes.findIndex((w) => w.pick(sources));
  return (
    <aside className={styles.rail} aria-label="MongoDB writes for this stage">
      <div className={styles.title}>Written to MongoDB</div>
      {why && <div className={styles.why}>{why}</div>}
      {writes.map((w, i) => (
        <WriteRow
          key={`${w.collection}-${w.op}-${i}`}
          write={w}
          sources={sources}
          open={i === firstWithDoc}
        />
      ))}
      {collapsed && (
        <button type="button" className={styles.link} onClick={() => setExpanded(false)}>
          Hide MongoDB detail
        </button>
      )}
    </aside>
  );
}
