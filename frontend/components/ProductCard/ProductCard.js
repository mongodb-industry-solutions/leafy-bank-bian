import Link from "next/link";
import Image from "next/image";
import { Subtitle } from "@leafygreen-ui/typography";
import Card from "@leafygreen-ui/card";
import styles from "./ProductCard.module.css";

/**
 * Wrapper component for a product card displayed on the home page.
 *
 * Props:
 *   href      - link target for the entire card
 *   imgSrc    - URL for the product icon/image
 *   imgAlt    - alt text for the image
 *   title     - heading text shown next to the image
 *   actionButton - optional button element to display in header
 *   children  - any additional content to render below the header
 *   locked    - when true, the card does not navigate to `href`; it renders a
 *               lock cue and calls `onLockedClick` instead (used to gate
 *               consent-protected panels behind an active Open Banking consent)
 *   onLockedClick - handler invoked when a locked card is activated
 */
export default function ProductCard({
  href,
  imgSrc,
  imgAlt,
  title,
  actionButton,
  children,
  locked = false,
  onLockedClick,
}) {
  const header = (
    <div className={styles.productHeader}>
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
      {actionButton && (
        <div className={styles.productHeaderAction} onClick={(e) => e.stopPropagation()}>
          {actionButton}
        </div>
      )}
    </div>
  );

  const inner = (
    <Card className={styles.leafyCard}>
      <div className={styles.productInner}>
        {header}
        {children}
      </div>
    </Card>
  );

  if (locked) {
    // Interactive only when a handler is provided; otherwise a plain,
    // non-focusable container so it isn't announced as a disabled button.
    const interactiveProps = onLockedClick
      ? {
          role: "button",
          tabIndex: 0,
          onClick: onLockedClick,
          onKeyDown: (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onLockedClick();
            }
          },
        }
      : {};

    return (
      <div className={`${styles.card} ${styles.cardProduct}`}>
        <div className={styles.cardLink} {...interactiveProps}>
          {inner}
        </div>
      </div>
    );
  }

  return (
    <div className={`${styles.card} ${styles.cardProduct}`}>
      <Link href={href} className={styles.cardLink}>
        {inner}
      </Link>
    </div>
  );
}
