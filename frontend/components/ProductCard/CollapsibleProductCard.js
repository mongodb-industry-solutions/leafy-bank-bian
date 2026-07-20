"use client";

import Image from "next/image";
import { Subtitle } from "@leafygreen-ui/typography";
import Card from "@leafygreen-ui/card";
import styles from "./ProductCard.module.css";

/**
 * Collapsible variant of the product card used on the home page.
 *
 * Renders a header (image + title) only — collapsed, with no chevron toggle and
 * not clickable, so clicking the card does nothing. Used for Credit Cards and
 * Loans when no external bank is connected.
 */
export default function CollapsibleProductCard({
  imgSrc,
  imgAlt,
  title,
}) {
  return (
    <div className={styles.collapsibleCard}>
      <Card className={styles.collapsibleLeafyCard}>
        <div className={styles.collapsibleHeader}>
          <div className={styles.productHeaderLeft}>
            <Image
              src={imgSrc}
              alt={imgAlt}
              width={48}
              height={48}
              className={styles.productImage}
            />
            <Subtitle>{title}</Subtitle>
          </div>
        </div>
      </Card>
    </div>
  );
}
