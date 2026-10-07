"use client";

import { useRouter } from "next/navigation";
import Card from "@leafygreen-ui/card";
import Badge from "@leafygreen-ui/badge";
import Icon from "@leafygreen-ui/icon";
import { H1, H3, Body, Overline } from "@leafygreen-ui/typography";
import LifecycleDiagram from "./LifecycleDiagram";
import styles from "./PaymentsHome.module.css";

const FEATURES = [
  {
    icon: "Wizard", title: "Payments Workflow", href: "/payments-workflow",
    body: "Initiate a wire or internal transfer and follow it through all nine stages, document by document.",
    badge: { variant: "green", text: "Live lifecycle" },
  },
  {
    icon: "Bulb", title: "Agentic Showcase", href: "/showcase",
    body: "Watch an agent investigate reconciliation breaks and cutoff risk, then decide.",
    badge: { variant: "blue", text: "AI-assisted" },
  },
  {
    icon: "Charts", title: "GL Pipeline Monitor", href: "/gl-pipeline-monitor",
    body: "See change streams turn ledger events into sub-ledger postings and balanced journals in real time.",
    badge: { variant: "lightgray", text: "Change streams" },
  },
];

export default function PaymentsHome({ bianModelUrl }) {
  const router = useRouter();

  return (
    <div className={styles.page}>
      <section className={styles.hero}>
        <Overline className={styles.eyebrow}>BIAN-aligned · ISO 20022 · MongoDB Atlas</Overline>
        <H1 className={styles.title}>Leafy Bank</H1>
        <H3 as="p" className={styles.tagline}>Payments Lifecycle Explorer</H3>
        <Body className={styles.subtitle}>
          One document model carries a payment from initiation to reconciliation. ACID settlement up
          front, a change-stream ledger behind it.
        </Body>
        <div className={styles.badges}>
          <Badge variant="darkgray">MongoDB-Powered</Badge>
          <Badge variant="green">9 Stages</Badge>
          <Badge variant="blue">Agent-Assisted Exceptions</Badge>
        </div>
      </section>

      <section className={styles.features}>
        {FEATURES.map((f) => (
          <Card key={f.href} className={styles.featureCard} onClick={() => router.push(f.href)}>
            <Icon glyph={f.icon} size="xlarge" className={styles.featureIcon} />
            <H3>{f.title}</H3>
            <Body className={styles.featureBody}>{f.body}</Body>
            <Badge variant={f.badge.variant}>{f.badge.text}</Badge>
          </Card>
        ))}
        <Card
          className={styles.featureCard}
          onClick={() => window.open(`${bianModelUrl}/bian-data-model?demo=leafy-bank`, "_blank", "noopener")}
        >
          <Icon glyph="Database" size="xlarge" className={styles.featureIcon} />
          <H3>BIAN Data Model</H3>
          <Body className={styles.featureBody}>
            Explore the service domains and document schemas behind every stage.
          </Body>
          <Badge variant="yellow">BIAN v14</Badge>
        </Card>
      </section>

      <section className={styles.lifecycle} aria-label="Payment lifecycle">
        <LifecycleDiagram />
      </section>
    </div>
  );
}
