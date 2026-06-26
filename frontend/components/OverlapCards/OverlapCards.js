"use client";

import React, { useState } from "react";
import styles from "./OverlapCards.module.css";
import Card from "@leafygreen-ui/card";
import Badge from "@leafygreen-ui/badge";
import { Subtitle, Body } from "@leafygreen-ui/typography";

// Leafy Bank accounts get a green badge; any other (external) bank gets blue.
const bankBadgeVariant = (bank) =>
  (bank || "").toLowerCase().replace(/\s/g, "") === "leafybank" ? "green" : "blue";

export default function OverlapCards({ items = [] }) {
  // Handle empty state
  if (!items || items.length === 0) {
    return (
      <div className={styles.container}>
        <Card className={styles.card}>
          <div className={styles.cardInner}>
            <div>
              <Subtitle>No items available</Subtitle>
              <Body className={styles.gray}>Check back later</Body>
            </div>
          </div>
        </Card>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      {items.map((it, idx) => (
        <Card key={idx} className={styles.card}>
          <div className={styles.cardInner}>
            <div>
              <Subtitle>{it.title}</Subtitle>
              <Body className={styles.gray}>{it.subtitle || `Account Number: ${it.number}`}</Body>
              {it.bank && (
                <Badge variant={bankBadgeVariant(it.bank)} className={styles.bankBadge}>
                  {it.bank}
                </Badge>
              )}
            </div>

            {it.amount !== undefined && (
              <div className={styles.amount}>
                USD {it.amount.toLocaleString()}
              </div>
            )}
          </div>
        </Card>
      ))}
    </div>
  );
}

