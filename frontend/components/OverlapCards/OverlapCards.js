"use client";

import React, { useState } from "react";
import styles from "./OverlapCards.module.css";
import Card from "@leafygreen-ui/card";
import Badge from "@leafygreen-ui/badge";
import { Subtitle, Body } from "@leafygreen-ui/typography";

// Leafy Bank accounts get a green badge; any other (external) bank gets blue.
const bankBadgeVariant = (bank) =>
  (bank || "").toLowerCase().replace(/\s/g, "") === "leafybank" ? "green" : "blue";

export default function OverlapCards({ items = [], onSelect, selectedKey }) {
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

  const clickable = typeof onSelect === "function";

  return (
    <div className={styles.container}>
      {items.map((it, idx) => {
        const isActive = clickable && selectedKey != null && selectedKey === it.number;
        const cardBody = (
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
        );

        if (!clickable) {
          return (
            <Card key={idx} className={styles.card}>
              {cardBody}
            </Card>
          );
        }

        return (
          <Card
            key={idx}
            className={`${styles.card} ${styles.clickable} ${isActive ? styles.active : ""}`}
            role="button"
            tabIndex={0}
            aria-pressed={isActive}
            onClick={() => onSelect(isActive ? null : it)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelect(isActive ? null : it);
              }
            }}
          >
            {cardBody}
          </Card>
        );
      })}
    </div>
  );
}

