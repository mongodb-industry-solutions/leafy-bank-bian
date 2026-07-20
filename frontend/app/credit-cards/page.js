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
                <ConsentGatedChart chartId="8867e720-081f-4b5a-9302-fb9b2b3622db" />
              </div>
            )}
            {selectedUser?.bankUsername === 'gracehop' && (
              <div className={styles.iframeWrap}>
                <ConsentGatedChart chartId="c5fc1948-d42d-4e46-a3c2-3e0c3cb1e637" />
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
                    <ConsentGatedChart chartId="fdc4b222-d67f-44d1-8809-767eae9e4f8a" />
                  </div>
                )}
                {selectedUser?.bankUsername === 'gracehop' && (
                  <div className={styles.iframeWrap}>
                    <ConsentGatedChart chartId="62d1db18-3a11-4806-b5b6-3fbdd5482f45" />
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
