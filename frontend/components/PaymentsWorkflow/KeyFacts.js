// A short label/value grid: the few facts a business reader needs before any detail.

import styles from "./StageVisuals.module.css";

export default function KeyFacts({ facts }) {
  if (facts.length < 2) return null;
  return (
    <dl className={styles.facts}>
      {facts.map(([label, value, tone]) => (
        <div className={styles.fact} key={label}>
          <dt className={styles.factLabel}>{label}</dt>
          <dd className={`${styles.factValue} ${tone === "warn" ? styles.factWarn : ""}`}>
            {String(value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}
