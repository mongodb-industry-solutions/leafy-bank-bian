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
 */
export default function ProductCard({ href, imgSrc, imgAlt, title, actionButton, children }) {
  return (
    <div className={`${styles.card} ${styles.cardProduct}`}>
      <Link href={href} className={styles.cardLink}>
        <Card className={styles.leafyCard}>
          <div className={styles.productInner}>
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

            {children}
          </div>
        </Card>
      </Link>
    </div>
  );
}
