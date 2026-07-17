"use client";

import { useState } from "react";
import Link from "next/link";
import Image from "next/image";
import { Subtitle } from "@leafygreen-ui/typography";
import Card from "@leafygreen-ui/card";
import IconButton from "@leafygreen-ui/icon-button";
import Icon from "@leafygreen-ui/icon";
import styles from "./ProductCard.module.css";

/**
 * Collapsible variant of the product card used on the home page.
 *
 * Renders a header (image + title) that links to the product page, plus a
 * chevron toggle that expands/collapses the card body. Used for Credit Cards
 * and Loans when no external bank is connected, to keep the view compact.
 *
 * Props mirror ProductCard, with:
 *   defaultOpen - whether the card starts expanded (default: false)
 */
export default function CollapsibleProductCard({
  href,
  imgSrc,
  imgAlt,
  title,
  defaultOpen = false,
  children,
}) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div className={styles.collapsibleCard}>
      <Card className={styles.collapsibleLeafyCard}>
        <div className={styles.collapsibleHeader}>
          <Link href={href} className={styles.collapsibleTitleLink}>
            <Image
              src={imgSrc}
              alt={imgAlt}
              width={48}
              height={48}
              className={styles.productImage}
            />
            <Subtitle>{title}</Subtitle>
          </Link>
          <IconButton
            aria-label={open ? `Collapse ${title}` : `Expand ${title}`}
            aria-expanded={open}
            onClick={() => setOpen((prev) => !prev)}
          >
            <Icon glyph={open ? "ChevronDown" : "ChevronRight"} />
          </IconButton>
        </div>

        {open && <div className={styles.collapsibleContent}>{children}</div>}
      </Card>
    </div>
  );
}
