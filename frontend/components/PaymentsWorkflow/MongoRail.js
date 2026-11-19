"use client";

// The MongoDB rail beside a lifecycle stage: why MongoDB fits this stage, then one row per
// collection the stage writes (operation badge, what changed, live document). The first row
// with a document opens expanded. In the Business lens the rail collapses to a one-line
// summary the presenter can expand. Capability chips sit above the writes; writes that commit
// in one ACID transaction are boxed together. Below the writes, any aggregation pipelines the stage runs.

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
      ) : write.pick.untracked ? (
        <Body className={styles.empty}>Written at this stage; this page does not load the document.</Body>
      ) : (
        <Body className={styles.empty}>Not written yet. It appears here as soon as it lands.</Body>
      )}
    </details>
  );
}

function QueryRow({ query }) {
  return (
    <details className={styles.row}>
      <summary className={styles.summary}>
        <span className={styles.head}>
          <span className={styles.collection}>{query.collection}</span>
          <Badge variant="yellow">aggregate</Badge>
        </span>
        <span className={styles.fields}>{query.title}</span>
        <span className={styles.via}>{query.when}</span>
      </summary>
      <div className={styles.code}>
        <Code language="javascript" copyButtonAppearance="hover">{query.pipeline}</Code>
      </div>
      <Body className={styles.empty}>Source: {query.source}</Body>
    </details>
  );
}

function Capabilities({ items }) {
  const [selected, setSelected] = useState(null);
  const active = selected != null ? items[selected] : null;
  return (
    <div className={styles.capabilities}>
      <div className={styles.chips}>
        {items.map((c, i) => (
          <button
            key={`${c.label}-${c.source}`}
            type="button"
            className={`${styles.chip} ${i === selected ? styles.chipActive : ""}`}
            aria-pressed={i === selected}
            onClick={() => setSelected(i === selected ? null : i)}
          >
            {c.label}
          </button>
        ))}
      </div>
      {active && (
        <div className={styles.chipDetail}>
          <span>{active.detail}</span>
          <code className={styles.source}>backend/{active.source}</code>
        </div>
      )}
    </div>
  );
}

// Adjacent writes sharing a transaction become one segment; the rest stand alone.
function segmentsOf(writes) {
  return writes.reduce((segments, write, index) => {
    const last = segments[segments.length - 1];
    if (write.txn && last?.txn === write.txn) last.items.push({ write, index });
    else segments.push({ txn: write.txn, items: [{ write, index }] });
    return segments;
  }, []);
}

function TxnGroup({ txn, count, collections, children }) {
  return (
    <div className={styles.txn}>
      <div className={styles.txnHead}>
        <span className={styles.txnLabel}>ACID transaction · {txn.label}</span>
        <span className={styles.txnMeta}>
          {count} writes in {collections} collection{collections === 1 ? "" : "s"}: all commit or none do
        </span>
        <code className={styles.source}>backend/{txn.source}</code>
      </div>
      {children}
    </div>
  );
}

export default function MongoRail({ writes, queries = [], capabilities = [], why, sources, collapsed }) {
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
  const row = ({ write, index }) => (
    <WriteRow
      key={`${write.collection}-${write.op}-${index}`}
      write={write}
      sources={sources}
      open={index === firstWithDoc}
    />
  );
  return (
    <aside className={styles.rail} aria-label="MongoDB writes for this stage">
      <div className={styles.title}>Written to MongoDB</div>
      {why && <div className={styles.why}>{why}</div>}
      {capabilities.length > 0 && <Capabilities items={capabilities} />}
      {segmentsOf(writes).map((seg) => (seg.txn && seg.items.length > 1 ? (
        <TxnGroup
          key={`txn-${seg.items[0].index}`}
          txn={seg.txn}
          count={seg.items.length}
          collections={new Set(seg.items.map((it) => it.write.collection)).size}
        >
          {seg.items.map(row)}
        </TxnGroup>
      ) : seg.items.map(row)))}
      {queries.length > 0 && (
        <>
          <div className={styles.title}>Aggregation pipelines run</div>
          {queries.map((q) => <QueryRow key={q.source} query={q} />)}
        </>
      )}
      {collapsed && (
        <button type="button" className={styles.link} onClick={() => setExpanded(false)}>
          Hide MongoDB detail
        </button>
      )}
    </aside>
  );
}
