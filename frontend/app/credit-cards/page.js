"use client";

import React from "react";
import styles from "./page.module.css";
import { H2, Body, Subtitle } from "@leafygreen-ui/typography";
import Card from "@leafygreen-ui/card";
import IconButton from "@leafygreen-ui/icon-button";
import Code from "@leafygreen-ui/code";
import TransactionsTable from "@/components/TransactionsTable/TransactionsTable";
import OverlapCards from "../../components/OverlapCards/OverlapCards";
import MobileActions from "@/components/MobileActions/MobileActions";
import { useCreditCardsPageData } from "@/lib/api/hooks";
import { useUser } from "@/lib/context/UserContext";
import ConsentGatedChart from "@/components/ConsentGatedChart/ConsentGatedChart";
import { chartIds } from "@/lib/config/charts";



export default function CreditCardsPage() {
  const { selectedUser } = useUser();
  const { creditCards, cardTxns, accountsLoading, txLoading } = useCreditCardsPageData();
  

  return (
    <main className={styles.container}>
      <H2>Credit Cards Overview</H2>

      <section className={styles.topSection}>
        <div className={styles.rowThree}>
          <Card className={styles.topCard}>
            {accountsLoading ? (
              <Body>Loading cards...</Body>
            ) : (
              <div className={styles.scrollWrapper}>
                <OverlapCards items={creditCards} />
              </div>
            )}
          </Card>
          <Card className={styles.topCard}>
            {selectedUser?.bankUsername === 'fridaklo' && (
              <div className={styles.iframeWrap}>
                <ConsentGatedChart chartId={chartIds.fridaklo.top} />
              </div>
            )}
            {selectedUser?.bankUsername === 'gracehop' && (
              <div className={styles.iframeWrap}>
                <ConsentGatedChart chartId={chartIds.gracehop.top} />
              </div>
            )}
            {(!selectedUser?.bankUsername || (selectedUser?.bankUsername !== 'fridaklo' && selectedUser?.bankUsername !== 'gracehop')) && (
              <div className={styles.iframeWrap}></div>
            )}
          </Card>

          <div className={styles.stackColumn}>
            <Card className={styles.stackTopCard}>
              <div className={styles.stackTopInner}>
                {selectedUser?.bankUsername === 'fridaklo' && (
                  <div className={styles.iframeWrap}>
                    <ConsentGatedChart chartId={chartIds.fridaklo.lower} />
                  </div>
                )}
                {selectedUser?.bankUsername === 'gracehop' && (
                  <div className={styles.iframeWrap}>
                    <ConsentGatedChart chartId={chartIds.gracehop.lower} />
                  </div>
                )}
                {(!selectedUser?.bankUsername || (selectedUser?.bankUsername !== 'fridaklo' && selectedUser?.bankUsername !== 'gracehop')) && (
                  <Subtitle>Other analytics</Subtitle>
                )}
              </div>
            </Card>
          </div>
        </div>
      </section>

      <section className={styles.bottomSection}>
        <H2>Transactions</H2>
        <TransactionsTable
          transactions={cardTxns}
          loading={txLoading}
        />
      </section>

      {/* Mobile-only bottom navigation + its action modals. */}
      <MobileActions />
    </main>
  );
}
