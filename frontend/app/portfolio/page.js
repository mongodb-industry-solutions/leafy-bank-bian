"use client";

import React, { useState } from 'react';
import { H1, H2, Subtitle } from '@leafygreen-ui/typography';
import Card from '@leafygreen-ui/card';
import { SegmentedControl, SegmentedControlOption } from '@leafygreen-ui/segmented-control';
import styles from './page.module.css';
import MobileActions from "@/components/MobileActions/MobileActions";
import LeafyBankAssistant from "../../components/LeafyBankAssistant/LeafyBankAssistant";

const Portfolio = () => {
    const [stockTimeframe, setStockTimeframe] = useState('6month');
    const [cryptoTimeframe, setCryptoTimeframe] = useState('6month');

    return (
        <div className={styles.page}>

            {/* ── STOCK PORTFOLIO ── */}
            <section className={styles.section}>
                <div className={styles.sectionHeader}>
                    <H2 className={styles.sectionTitle}>Stock Portfolio</H2>
                </div>

                <div className={styles.gridContainer}>
                    <Card className={styles.roiCard}>
                        <Subtitle className={styles.cardSubtitle}>Portfolio Performance</Subtitle>
                        <div className={styles.roiHeader}>
                            <SegmentedControl
                                followFocus={true}
                                defaultValue="6month"
                                value={stockTimeframe}
                                onChange={(value) => setStockTimeframe(value)}
                                className={styles.segmentedControl}
                            >
                                <SegmentedControlOption value="month">This Month</SegmentedControlOption>
                                <SegmentedControlOption value="6month">Last 6 Months</SegmentedControlOption>
                            </SegmentedControl>
                        </div>
                        <div className={styles.iframeContainer}>
                            {stockTimeframe === 'month' && (
                                <iframe
                                    className={styles.responsiveIframe}
                                    src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=9de04b25-cc2f-48bb-94bd-3c3d5c6ffb20&maxDataAge=3600&theme=light&autoRefresh=true"
                                />
                            )}
                            {stockTimeframe === '6month' && (
                                <iframe
                                    className={styles.responsiveIframe}
                                    src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=3658d399-d4d8-4253-abe6-833a366ad30c&maxDataAge=3600&theme=light&autoRefresh=true"
                                />
                            )}
                        </div>
                    </Card>

                    <div className={styles.pieCharts}>
                        <Card className={styles.assetCard}>
                            <Subtitle className={styles.cardSubtitle}>Asset Distribution by Symbol</Subtitle>
                            <div className={styles.iframeContainer}>
                                <iframe
                                    className={styles.responsiveIframe}
                                    src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=cd8d523c-b90a-4a39-a447-2e53cd392924&maxDataAge=3600&theme=light&autoRefresh=true"
                                />
                            </div>
                        </Card>
                        <Card className={styles.assetCard}>
                            <Subtitle className={styles.cardSubtitle}>Asset Distribution by Type</Subtitle>
                            <div className={styles.iframeContainer}>
                                <iframe
                                    className={styles.responsiveIframe}
                                    src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=5af0765f-51bc-47af-ae5f-92cff4fadadb&maxDataAge=3600&theme=light&autoRefresh=true"
                                />
                            </div>
                        </Card>
                    </div>
                </div>
            </section>

            {/* ── CRYPTO PORTFOLIO ── */}
            <section className={styles.section}>
                <div className={styles.sectionHeader}>
                    <H2 className={styles.sectionTitle}>Crypto Portfolio</H2>
                </div>

                <div className={styles.gridContainer}>
                    <Card className={styles.roiCard}>
                        <Subtitle className={styles.cardSubtitle}>Portfolio Performance</Subtitle>
                        <div className={styles.roiHeader}>
                            <SegmentedControl
                                followFocus={true}
                                defaultValue="6month"
                                value={cryptoTimeframe}
                                onChange={(value) => setCryptoTimeframe(value)}
                                className={styles.segmentedControl}
                            >
                                <SegmentedControlOption value="month">This Month</SegmentedControlOption>
                                <SegmentedControlOption value="6month">Last 6 Months</SegmentedControlOption>
                            </SegmentedControl>
                        </div>
                        <div className={styles.iframeContainer}>
                            {cryptoTimeframe === 'month' && (
                                <iframe
                                    className={styles.responsiveIframe}
                                    src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=9de04b25-cc2f-48bb-94bd-3c3d5c6ffb20&maxDataAge=3600&theme=light&autoRefresh=true"
                                />
                            )}
                            {cryptoTimeframe === '6month' && (
                                <iframe
                                    className={styles.responsiveIframe}
                                    src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=3658d399-d4d8-4253-abe6-833a366ad30c&maxDataAge=3600&theme=light&autoRefresh=true"
                                />
                            )}
                        </div>
                    </Card>

                    <div className={styles.pieCharts}>
                        <Card className={styles.assetCard}>
                            <Subtitle className={styles.cardSubtitle}>Crypto Asset Distribution by Symbol</Subtitle>
                            <div className={styles.iframeContainer}>
                                <iframe
                                    className={styles.responsiveIframe}
                                    src="https://charts.mongodb.com/charts-jeffn-zsdtj/embed/charts?id=cfd11f4a-b8b8-446d-91fe-ba8c03bc3ce9&maxDataAge=3600&theme=light&autoRefresh=true"
                                />
                            </div>
                        </Card>
                    </div>
                </div>
            </section>


            {/* Mobile-only bottom navigation + its action modals. */}
            <MobileActions />

        </div>
    );
};

export default Portfolio;