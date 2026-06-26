"use client";

import React, { useState } from "react";
import styles from "./page.module.css";
import { H2, Body, Subtitle } from "@leafygreen-ui/typography";
import Card from "@leafygreen-ui/card";
import Image from "next/image";
import Icon from "@leafygreen-ui/icon";

import OverlapCards from "../../components/OverlapCards/OverlapCards";
import LeafyBankAssistant from "../../components/LeafyBankAssistant/LeafyBankAssistant";
import IconButton from "@leafygreen-ui/icon-button";
import Code from "@leafygreen-ui/code";
import TransactionsTable from "@/components/TransactionsTable/TransactionsTable";
import MobileActions from "@/components/MobileActions/MobileActions";
import { useAccountsPageData } from "@/lib/api/hooks";
import { useUser } from "@/lib/context/UserContext";



export default function AccountsPage() {
  const [modalOpen, setModalOpen] = useState(false);
  const { selectedUser } = useUser();
  const { allAccounts, recentTxns, accountsLoading, txLoading } = useAccountsPageData();
  
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
                <OverlapCards items={allAccounts.length > 0 ? allAccounts : []} />
              </div>
            )}
          </Card>
          <Card className={styles.topCard}>
            {selectedUser?.name === 'fridaklo' && (
              <div className={styles.iframeWrap}>
                <iframe
                  width="640"
                  height="480"
                  src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=1066e97f-6628-49be-a720-462c0d87d32c&maxDataAge=3600&theme=light&autoRefresh=true"
                ></iframe>
              </div>
            )}
            {selectedUser?.name === 'hellyrig' && (
              <div className={styles.iframeWrap}>
                <iframe
                  width="640"
                  height="480"
                  src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=45137520-16e5-430a-8a12-b07deca1b69e&maxDataAge=3600&theme=light&autoRefresh=true"
                ></iframe>
              </div>
            )}
            {(!selectedUser?.name || (selectedUser?.name !== 'fridaklo' && selectedUser?.name !== 'hellyrig')) && (
              <div className={styles.iframeWrap}></div>
            )}
          </Card>

          <div className={styles.stackColumn}>
            <Card className={styles.stackTopCard}>
              <div className={styles.stackTopInner}>
                {selectedUser?.name === 'fridaklo' && (
                  <div className={styles.iframeWrap}>
                    <iframe
                      width="640"
                      height="480"
                      src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=b2f07682-5ce1-4955-9e7f-703ba881404b&maxDataAge=3600&theme=light&autoRefresh=true"
                    ></iframe>
                  </div>
                )}
                {selectedUser?.name === 'hellyrig' && (
                  <div className={styles.iframeWrap}>
                    <iframe
                      width="640"
                      height="480"
                      src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=b421bdc8-3f1f-42ec-ac02-91196c36a1dd&maxDataAge=3600&theme=light&autoRefresh=true"
                    ></iframe>
                  </div>
                )}
                {(!selectedUser?.name || (selectedUser?.name !== 'fridaklo' && selectedUser?.name !== 'hellyrig')) && (
                  <Subtitle>Other analytics</Subtitle>
                )}
              </div>
            </Card>

            <Card className={styles.stackBottomCard}>
              <button
                onClick={() => setModalOpen(true)}
                style={{
                  background: "transparent",
                  border: "none",
                  padding: 0,
                  cursor: "pointer",
                  width: "100%",
                }}
                aria-label="Add other financial entities"
              >
                <div className={styles.cardContent}>
                  <div className={styles.thumbWrap}>
                    <Image
                      src="/tips.gif"
                      alt="thumbnail"
                      width={90}
                      height={56}
                    />
                  </div>

                  <div className={styles.cardText}>
                    <Subtitle>Join our Financial Program</Subtitle>
                    <Body className={styles.cardBodyGray}>
                      Get more out of Leafy Bank
                    </Body>
                  </div>

                  <div className={styles.iconRight}>
                    <Icon glyph="ChevronRight" size="small" />
                  </div>
                </div>
              </button>
            </Card>
          </div>
        </div>
      </section>

      <section className={styles.bottomSection}>
        <H2>Transactions</H2>
        <TransactionsTable transactions={recentTxns} loading={txLoading} />
      </section>

      <LeafyBankAssistant
        isOpen={modalOpen}
        onClose={() => setModalOpen(false)}
      />

      {/* Mobile-only bottom navigation + its action modals. */}
      <MobileActions />
    </main>
  );
}
