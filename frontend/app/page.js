"use client";

import React, { useState, useEffect } from "react";
import styles from "./page.module.css";
import { H2, H3, Body, Subtitle } from "@leafygreen-ui/typography";
import Card from "@leafygreen-ui/card";
import Badge from "@leafygreen-ui/badge";
import Button from "@leafygreen-ui/button";
import Image from "next/image";
import Icon from "@leafygreen-ui/icon";
import Link from "next/link";
import ProductCard from "@/components/ProductCard/ProductCard";
import CollapsibleProductCard from "@/components/ProductCard/CollapsibleProductCard";
import LeafyBankAssistant from "../components/LeafyBankAssistant/LeafyBankAssistant";
import OpenFinanceAssistant from "@/components/OpenFinanceAssistant/OpenFinanceAssistant";
import Login from "@/components/Login/Login";
import AccountModal from "@/components/AccountModal/AccountModal";
import SendMoneyModal from "@/components/SendMoneyModal/SendMoneyModal";
import TransactionsTable from "@/components/TransactionsTable/TransactionsTable";
import BottomNav from "@/components/BottomNav/BottomNav";
import { useUser } from "@/lib/context/UserContext";
import { useHomeData, useAccountsPageData } from "@/lib/api/hooks";
import { formatCurrency } from "@/lib/api/format";
import MobileActions from "@/components/MobileActions/MobileActions";



// Leafy Bank accounts get a green badge; any other (external) bank gets blue.
const isExternalBank = (bank) =>
  (bank || "").toLowerCase().replace(/\s/g, "") !== "leafybank";

const bankBadgeVariant = (bank) => (isExternalBank(bank) ? "blue" : "green");

const HomeContent = () => {
  const [modalOpen, setModalOpen] = useState(false);
  const { selectedUser, authorizedConsents } = useUser();
  const hasExternalBank = authorizedConsents.length > 0;
  const { totalBalance, totalDebt, bankAccounts, creditCards, creditScore, loans, loading: homeLoading } = useHomeData();
  const { recentTxns, txLoading } = useAccountsPageData();
  const [pendingPrompt, setPendingPrompt] = useState(null);
  const [accountModalOpen, setAccountModalOpen] = useState(false);
  const [accountBalance, setAccountBalance] = useState("");
  const [accountType, setAccountType] = useState("");
  const [sendMoneyOpen, setSendMoneyOpen] = useState(false);
  const [openFinanceOpen, setOpenFinanceOpen] = useState(false);

  const accountsContent = (
    <div className={styles.accountList}>
      {homeLoading ? (
        <Body className={styles.cardBodyGray}>Loading...</Body>
      ) : bankAccounts.length > 0 ? (
        // Show external accounts (Green Bank, MongoDB Bank, NeoFinance,
        // …) first; stable sort keeps original order within each group.
        [...bankAccounts]
          .sort(
            (a, b) =>
              isExternalBank(b.AccountBank) - isExternalBank(a.AccountBank)
          )
          .map((account) => (
            <div key={account._id} className={styles.accountRow}>
              <div className={styles.accountInfo}>
                <Body>{account.AccountType} Account</Body>
                <Body className={styles.cardBodyGray}>
                  Account Number: {account.AccountNumber}
                </Body>
                {account.AccountBank && (
                  <Badge variant={bankBadgeVariant(account.AccountBank)} className={styles.accountBankBadge}>
                    {account.AccountBank}
                  </Badge>
                )}
              </div>
              <div className={styles.accountAmount}>
                <Subtitle>
                  {formatCurrency(account.AccountBalance)}
                </Subtitle>
              </div>
            </div>
          ))
      ) : (
        <Body className={styles.cardBodyGray}>No accounts found</Body>
      )}
    </div>
  );

  const openAccountButton = (
    <Button
      size="small"
      variant="default"
      leftGlyph={<Icon glyph="Plus" />}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        setAccountModalOpen(true);
      }}
    >
      Open <span className={styles.hideOnMobile}>Account</span>
    </Button>
  );

  const creditCardsContent = (
    <div className={styles.accountList}>
      {creditCards.map((card) => (
        <div key={card._id} className={styles.accountRow}>
          <div className={styles.accountInfo}>
            <Body>{card.AccountDescription || "Credit Card"}</Body>
            <Body className={styles.cardBodyGray}>{card.AccountNumber}</Body>
            {card.AccountBank && (
              <Badge variant={bankBadgeVariant(card.AccountBank)} className={styles.accountBankBadge}>
                {card.AccountBank}
              </Badge>
            )}
          </div>
          <div className={styles.accountAmount}>
            <Subtitle>{formatCurrency(card.AccountBalance)}</Subtitle>
          </div>
        </div>
      ))}
    </div>
  );

  const loansContent = (
    <div className={styles.accountList}>
      {loans.length > 0 ? (
        loans.map((loan, i) => (
          <div key={i} className={styles.accountRow}>
            <div className={styles.accountInfo}>
              <Body>{loan.name}</Body>
              {loan.institution && (
                <Badge variant={bankBadgeVariant(loan.institution)} className={styles.accountBankBadge}>
                  {loan.institution}
                </Badge>
              )}
            </div>
            <div className={styles.accountAmount}>
              <Subtitle>{formatCurrency(loan.balance)}</Subtitle>
            </div>
          </div>
        ))
      ) : (
        <Body className={styles.cardBodyGray}>
          Connect a bank via chatbot to see loans
        </Body>
      )}
    </div>
  );

  return (
    <main className={styles.container}>
      {/* Top 40% — only visible once an external bank is connected */}
      {hasExternalBank && <section className={styles.topSection}>
        <div className={styles.productsHeader}>
          <H2>Global Position</H2>
        </div>
        <div className={styles.gpRow}>
          <div className={`${styles.card} ${styles.cardEqual}`}>
            <Card className={styles.leafyCard}>
              <div className={styles.cardContent}>
                <div className={styles.thumbWrap}>
                  <Image src="/balance.png" alt="Total Balance" width={48} height={48} />
                </div>
                <div className={styles.cardText}>
                  <Subtitle>
                    {homeLoading ? "..." : formatCurrency(totalBalance)}
                  </Subtitle>
                  <Body className={styles.cardBodyGray}>Total Balance</Body>
                </div>
              </div>
            </Card>
          </div>
          <div className={`${styles.card} ${styles.cardEqual}`}>
            <Card className={styles.leafyCard}>
              <div className={styles.cardContent}>
                <div className={styles.thumbWrap}>
                  <Image src="/debt.png" alt="Total Debt" width={48} height={48} />
                </div>
                <div className={styles.cardText}>
                  <Subtitle>
                    {homeLoading ? "..." : formatCurrency(totalDebt)}
                  </Subtitle>
                  <Body className={styles.cardBodyGray}>Total Debt</Body>
                </div>
              </div>
            </Card>
          </div>
         
        </div>
        <div className={styles.sectionDots}>
          <span className={styles.sectionDot} />
          <span className={styles.sectionDot} />
        </div>
      </section>}

      {/* Bottom 40% */}
      <section className={styles.bottomSection}>
        <div className={styles.productsHeader}>
          <H2>Products</H2>
        </div>
        {hasExternalBank ? (
          <div className={styles.rowThreeEqual}>
            <ProductCard
              href="/accounts"
              imgSrc="/bank.png"
              imgAlt="accounts"
              title="Accounts"
              actionButton={openAccountButton}
            >
              {accountsContent}
            </ProductCard>

            <ProductCard href="/credit-cards" imgSrc="/card.png" imgAlt="credit-cards" title="Credit Cards">
              {creditCardsContent}
            </ProductCard>

            <ProductCard href="/loans" imgSrc="/loan.png" imgAlt="loans" title="Loans">
              {loansContent}
            </ProductCard>
          </div>
        ) : (
          <div className={styles.stackedProducts}>
            <ProductCard
              href="/accounts"
              imgSrc="/bank.png"
              imgAlt="accounts"
              title="Accounts"
              actionButton={openAccountButton}
            >
              {accountsContent}
            </ProductCard>

            <CollapsibleProductCard href="/credit-cards" imgSrc="/card.png" imgAlt="credit-cards" title="Credit Cards">
              {creditCardsContent}
            </CollapsibleProductCard>

            <CollapsibleProductCard href="/loans" imgSrc="/loan.png" imgAlt="loans" title="Loans">
              {loansContent}
            </CollapsibleProductCard>
          </div>
        )}
        <div className={styles.sectionDots}>
          <span className={styles.sectionDot} />
          <span className={styles.sectionDot} />
          <span className={styles.sectionDot} />
        </div>
      </section>

       {/* Middle 20% */}
      <section className={styles.midSection}>
        <div className={styles.rowTwo}>
        
          <div className={`${styles.card} ${styles.cardEqual}`}>
            <Card className={styles.leafyCard}>
              <button
                onClick={() => setOpenFinanceOpen(true)}
                className={styles.entitiesButton}
                aria-label="Get a complete view of your finances with Open Banking"
              >
                <div className={styles.cardContent}>
                  <div className={styles.thumbWrap}>
                    <Image
                      src="/credit_card.gif"
                      alt="thumbnail"
                      width={70}
                      height={70}
                    />
                  </div>

                  <div className={styles.cardText}>
                    <Subtitle>Get a complete view of your finances with Open Banking</Subtitle>
                    <Body className={styles.cardBodyGray}>
                      Aggregate your other banks accounts in one place and make smarter financial decisions.
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

      <section className={styles.activitySection}>
        <H3>Recent Activity</H3>
        <TransactionsTable transactions={recentTxns.slice(0, 50)} loading={txLoading} />
      </section>

      <div className={styles.stickyButtonContainer}>
        <Button
          variant="baseGreen"
          onClick={() => setSendMoneyOpen(true)}
        >
          Send Money
        </Button>
      </div>

      {/* Mobile-only bottom navigation. Icons are placeholders — swap the
          `icon` values for the final icons when provided. */}
       <MobileActions />

      <SendMoneyModal
        isOpen={sendMoneyOpen}
        onClose={() => setSendMoneyOpen(false)}
      />

      <AccountModal
        isOpen={accountModalOpen}
        onClose={() => setAccountModalOpen(false)}
        accountBalance={accountBalance}
        setAccountBalance={setAccountBalance}
        accountType={accountType}
        setAccountType={setAccountType}
      />

      <LeafyBankAssistant
        isOpen={modalOpen}
        onClose={() => setModalOpen(false)}
        initialPrompt={pendingPrompt}
      />

      <OpenFinanceAssistant
        isOpen={openFinanceOpen}
        onClose={() => setOpenFinanceOpen(false)}
      />
    </main>
  );
};

export default function Home() {
  const { selectedUser, clearUser, isFreshBrowserLoad } = useUser();
  const [loginDone, setLoginDone] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  // Genuine fresh browser load (refresh/new tab) → clear user so Login shows.
  // Client-side navigation (logo click) and intentional in-app full-page
  // navigations (backoffice personas) are preserved — see isFreshBrowserLoad.
  useEffect(() => {
    if (isFreshBrowserLoad()) {
      clearUser();
    }
  }, [isFreshBrowserLoad, clearUser]);

  // Sync loginDone with user selection state in both directions
  useEffect(() => {
    setLoginDone(!!selectedUser);
  }, [selectedUser]);

  if (!mounted) return null;

  return (
    <>
      {!loginDone && <Login onDone={() => setLoginDone(true)} />}
      {selectedUser && loginDone && <HomeContent />}
    </>
  );
}
