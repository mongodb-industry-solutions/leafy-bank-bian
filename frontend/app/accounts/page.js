"use client";

import React, { useState } from "react";
import styles from "./page.module.css";
import { H2, Body, Subtitle } from "@leafygreen-ui/typography";
import Card from "@leafygreen-ui/card";

import OverlapCards from "../../components/OverlapCards/OverlapCards";
import IconButton from "@leafygreen-ui/icon-button";
import Code from "@leafygreen-ui/code";
import TransactionsTable from "@/components/TransactionsTable/TransactionsTable";
import MobileActions from "@/components/MobileActions/MobileActions";
import { useAccountsPageData } from "@/lib/api/hooks";
import { useUser } from "@/lib/context/UserContext";
import ConsentGatedChart from "@/components/ConsentGatedChart/ConsentGatedChart";
import { chartIds } from "@/lib/config/charts";



export default function AccountsPage() {
  const [selectedAccount, setSelectedAccount] = useState(null);
  const { selectedUser } = useUser();
  const { allAccounts, recentTxns, accountsLoading, txLoading } = useAccountsPageData();

  // When an account card is selected, show only that account's transactions.
  const visibleTxns = (
    selectedAccount
      ? recentTxns.filter((t) =>
          t._accountKeys?.some(
            (k) => k === selectedAccount.id || k === selectedAccount.number
          )
        )
      : recentTxns
  ).slice(0, 50);

  return (
    <main className={styles.container}>
      <H2>Accounts Overview</H2>
      <section className={styles.topSection}>
        <div className={styles.rowThree}>
          <Card className={styles.topCard}>
            {accountsLoading ? (
              <Body>Loading accounts...</Body>
            ) : (
              <div className={styles.scrollWrapper}>
                <OverlapCards
                  items={allAccounts.length > 0 ? allAccounts : []}
                  onSelect={setSelectedAccount}
                  selectedKey={selectedAccount?.number ?? null}
                />
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
        <div className={styles.txnHeader}>
          <H2>Transactions</H2>
          {selectedAccount && (
            <div className={styles.txnFilter}>
              <Body className={styles.txnFilterLabel}>
                {selectedAccount.title} · {selectedAccount.number}
              </Body>
              <button
                type="button"
                className={styles.clearFilterBtn}
                onClick={() => setSelectedAccount(null)}
              >
                Show all
              </button>
            </div>
          )}
        </div>
        <TransactionsTable transactions={visibleTxns} loading={txLoading} />
      </section>

      {/* Mobile-only bottom navigation + its action modals. */}
      <MobileActions />
    </main>
  );
}
