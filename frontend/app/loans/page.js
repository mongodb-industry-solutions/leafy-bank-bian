"use client";

import React, { useState } from "react";
import styles from "./page.module.css";
import { H2, Body, Subtitle } from "@leafygreen-ui/typography";
import Card from "@leafygreen-ui/card";
import Badge from "@leafygreen-ui/badge";
import Image from "next/image";
import Icon from "@leafygreen-ui/icon";
import IconButton from "@leafygreen-ui/icon-button";
import Code from "@leafygreen-ui/code";
import OverlapCards from "../../components/OverlapCards/OverlapCards";
import LeafyBankAssistant from "../../components/LeafyBankAssistant/LeafyBankAssistant";
import MobileActions from "@/components/MobileActions/MobileActions";
import { useLoansPageData } from "@/lib/api/hooks";

// Leafy Bank gets a green badge; any other (external) bank gets blue.
const bankBadgeVariant = (bank) =>
  (bank || "").toLowerCase().replace(/\s/g, "") === "leafybank" ? "green" : "blue";

export default function LoansPage() {
  const [modalOpen, setModalOpen] = useState(false);
  const { loanCards, loanTableRows, loading, hasActiveConsent } = useLoansPageData();
  const [expandedRow, setExpandedRow] = useState(null);

  function LoanProgressCard({ row }) {
    const originalAmount = row.originalAmount || row.outstanding;
    const paid = originalAmount - row.outstanding;
    const percent = originalAmount > 0 ? ((paid / originalAmount) * 100).toFixed(2) : 0;

    let message = "";
    if (percent >= 90) message = "You're almost there!";
    else if (percent >= 50) message = "Halfway done!";
    else message = "Keep going!";

    return (
      <Card className={styles.progressCard}>
        <Body className={styles.productName}>{row.type}</Body>

        <div className={styles.progressBarBackground}>
          <div
            className={styles.progressBarFill}
            style={{ width: `${percent}%` }}
          />
          <span className={styles.progressPercent}>{percent}%</span>
        </div>

        <Body>{message}</Body>
      </Card>
    );
  }

  return (
    <main className={styles.container}>
      <H2>Loans Overview</H2>

      <section className={styles.topSection}>
        <div className={styles.rowThree}>
          <Card className={styles.topCard}>
            {loading ? (
              <Body>Loading loans...</Body>
            ) : loanCards.length > 0 ? (
              <div className={styles.scrollWrapper}>
                <OverlapCards items={loanCards} />

              </div>
            ) : (
              <div style={{ padding: "20px", textAlign: "center" }}>
                <Body className={styles.cardBodyGray}>
                  {hasActiveConsent
                    ? "No external loan products found"
                    : "Connect your external bank via the chatbot to see your loans"}
                </Body>
              </div>
            )}
          </Card>

          <Card className={styles.topCard}>
            {loading ? (
              <Body>Loading progress...</Body>
            ) : loanTableRows.length > 0 ? (
              <div className={styles.scrollWrapper}>
                {loanTableRows.map((row, i) => (
                  <LoanProgressCard key={i} row={row} />
                ))}
              </div>
            ) : (
              <div style={{ padding: "20px", textAlign: "center" }}>
                <Body className={styles.cardBodyGray}>
                  {hasActiveConsent
                    ? "No external loan products found"
                    : "Connect your external bank via the chatbot to see your loans"}
                </Body>
              </div>
            )}
          </Card>

          <div className={styles.stackColumn}>


            <Card className={styles.stackTopCard}>

              <Subtitle>Other Analytics</Subtitle>

              <div className={styles.stackTopInner}>
                {loading ? (
                  <Body>Loading analytics...</Body>
                ) : loanTableRows.length > 0 ? (
                  <>

                    <Card className={styles.kpiCard} >
                      <span className={styles.kpiNumber}> {loanTableRows.length}  </span>
                      <span className={styles.kpiLabel}>Active loans</span>
                    </Card>

                    <Card className={styles.kpiCard}>
                      <span className={styles.kpiNumber}>
                        {loanTableRows
                          .reduce((sum, row) => sum + row.outstanding, 0)
                          .toLocaleString(undefined, {
                            style: "currency",
                            currency: "USD",
                          })}
                      </span>
                      <span className={styles.kpiLabel}>Total outstanding</span>
                    </Card>

                  </>
                ) : (
                  <Body className={styles.cardBodyGray}>
                    {hasActiveConsent
                      ? "No analytics available"
                      : "Connect your external bank to see analytics"}
                  </Body>
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
                aria-label="Want to get a loan?"
              >
                <div className={styles.cardContent}>
                  <div className={styles.thumbWrap}>
                    <Image src="/loan.gif" alt="loan icon" width={80} height={56} />
                  </div>

                  <div className={styles.cardText}>
                    <Subtitle>Want to get a loan?</Subtitle>
                    <Body className={styles.cardBodyGray}>Find out if you&apos;re eligible</Body>
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
        <H2>Loan Details</H2>
        <div className={styles.tableWrap}>
          {loanTableRows.length > 0 ? (
            <table className={styles.table}>
              <thead>
                <tr>
                  <th className={styles.th}>Type</th>
                  <th className={styles.th}>Institution</th>
                  <th className={styles.th}>Contract</th>
                  <th className={styles.th} style={{ textAlign: "right" }}>
                    Outstanding
                  </th>
                </tr>
              </thead>
              <tbody>
                {loanTableRows.map((row, i) => (
                  <React.Fragment key={i}>
                    <tr key={i}>
                      <td>{row.type}</td>
                      <td>
                        {row.institution && (
                          <Badge variant={bankBadgeVariant(row.institution)}>
                            {row.institution}
                          </Badge>
                        )}
                      </td>
                      <td>{row.contract}</td>
                      <td style={{ textAlign: "right" }}>
                        {row.outstanding.toLocaleString(
                          undefined,
                          { style: "currency", currency: "USD" }
                        )}
                      </td>
                      <td>

                        <IconButton
                          aria-label={`View details for loan`}
                          className={styles.iconButton}
                          onClick={() =>
                            setExpandedRow(expandedRow === i ? null : i)
                          }>
                          <Icon glyph="CurlyBraces" />
                        </IconButton>

                      </td>
                    </tr>

                    {expandedRow === i && (
                      <tr className={styles.expandedRow}>
                        <td colSpan={6}>
                          <div className={styles.expandedContent}>
                            <Code language="json">{JSON.stringify(row, null, 2)}</Code>
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          ) : (
            <Body className={styles.cardBodyGray} style={{ padding: "20px" }}>
              {hasActiveConsent
                ? "No loan data available"
                : "Use the chatbot to connect your external bank and view loan details"}
            </Body>
          )}
        </div>
      </section>

      <LeafyBankAssistant isOpen={modalOpen} onClose={() => setModalOpen(false)} />

      {/* Mobile-only bottom navigation + its action modals. */}
      <MobileActions />
    </main>
  );
}
