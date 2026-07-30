"use client";

import React, { useState, useEffect, useRef } from 'react';
import Icon from '@leafygreen-ui/icon';
import { Modal, Container } from 'react-bootstrap';
import { H2, Description } from '@leafygreen-ui/typography';
import styles from './Login.module.css';
import User from '@/components/User/User';
import { USER_LIST } from "@/lib/constants";
import { useUser } from "@/lib/context/UserContext";

// Deliberately fixed rather than tied to any real request — this is a UX beat, not a
// loading state for actual work, so it doesn't need to reflect real latency. Used for
// the backoffice flow (which navigates to a different page or an external tab, so
// there's nothing on *this* page to sync against) and as a floor for the retail flow
// below, so a warm cache doesn't make the overlay feel like a flicker.
const LOGIN_TRANSITION_MS = 250;
// Retail-only: if `dataReady` never arrives (backend down, request hung), don't strand
// the user on the overlay forever.
const DATA_SYNC_SAFETY_TIMEOUT_MS = 6000;

const Login = ({ onDone, dataReady = false }) => {
    const { selectUser, setLoginInProgress } = useUser();
    const [open, setOpen] = useState(true);
    const [selectedLocal, setSelectedLocal] = useState(null);
    const [step, setStep] = useState('choose'); // 'choose' | 'backoffice'
    // Set while the "Logging in as ..." beat plays; cleared once selection/navigation
    // proceeds. Non-null also drives the overlay below.
    const [pendingUser, setPendingUser] = useState(null);
    const retailLoginStartedAt = useRef(null);

    // Shown before a user selection takes effect, whether that's closing the modal
    // (backoffice) or navigating away (backoffice personas with a `url`). Passed to
    // User.jsx as onBeforeSelect so both paths route through it. Not used by the
    // retail path — see handleRetailSelect below.
    const showLoginTransition = (user) =>
        new Promise((resolve) => {
            setPendingUser(user);
            setTimeout(resolve, LOGIN_TRANSITION_MS);
        });

    const backofficeUsers = USER_LIST.filter(u => u.section === 'backoffice');
    // Bank Customer logs in directly as Frida; switching users is done from the
    // NavBar dropdown afterwards.
    const defaultRetailUser =
        USER_LIST.find(u => u.section === 'retail' && u.name === 'Frida') ||
        USER_LIST.find(u => u.section === 'retail');

    const handleUserSelect = (user) => {
        setSelectedLocal(user);
        selectUser(user);
        setOpen(false);
        onDone?.();
    };

    const finalizeRetailLogin = () => {
        // selectUser already ran in handleRetailSelect (to start the fetch as early as
        // possible) — don't call it again here, it would needlessly reset consent/chat
        // state a second time for no behavioral gain.
        setPendingUser(null);
        setSelectedLocal(defaultRetailUser);
        setLoginInProgress(false);
        setOpen(false);
        onDone?.();
    };

    // Retail login lands on this same page's dashboard, so — unlike backoffice —
    // the overlay can wait on real data instead of a fixed timer. Selecting the user
    // immediately (rather than after a delay) lets the parent mount HomeContent right
    // away so its fetches start in parallel with the overlay, not after it. Setting
    // loginInProgress keeps NavBar (which reads selectedUser directly) from showing
    // the logged-in header while this modal is still on screen.
    const handleRetailSelect = () => {
        if (!defaultRetailUser) return;
        retailLoginStartedAt.current = Date.now();
        setPendingUser(defaultRetailUser);
        setLoginInProgress(true);
        selectUser(defaultRetailUser);
    };

    // Close the overlay once the dashboard's data has arrived, holding it open at
    // least LOGIN_TRANSITION_MS so an already-warm cache doesn't finish instantly.
    useEffect(() => {
        if (!dataReady || pendingUser !== defaultRetailUser) return;
        const elapsed = Date.now() - (retailLoginStartedAt.current ?? Date.now());
        const remaining = Math.max(0, LOGIN_TRANSITION_MS - elapsed);
        const t = setTimeout(finalizeRetailLogin, remaining);
        return () => clearTimeout(t);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [dataReady, pendingUser]);

    // Safety net: proceed anyway if the data never reports ready.
    useEffect(() => {
        if (dataReady || pendingUser !== defaultRetailUser) return;
        const t = setTimeout(finalizeRetailLogin, DATA_SYNC_SAFETY_TIMEOUT_MS);
        return () => clearTimeout(t);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [dataReady, pendingUser]);

    return (
        <Modal
            show={open}
            onHide={() => {
                if (!selectedLocal) {
                    alert("You must select a user before proceeding!");
                    return;
                }
                setOpen(false);
            }}
            size="lg"
            aria-labelledby="contained-modal-title-vcenter"
            centered
            fullscreen={'md-down'}
            className={styles.leafyFeel}
            backdrop="static"
        >
            <Container className={styles.modalContainer}>
                <div className={styles.modalHeader}>
                    {step !== 'choose' && (
                        <button className={styles.goBackBtn} onClick={() => setStep('choose')}>
                            <Icon glyph="ArrowLeft" /> Go back
                        </button>
                    )}
                    <div
                        className={`${styles.closeBtn} ${!selectedLocal ? styles.disabledCloseButton : ''}`}
                        onClick={() => {
                            if (!selectedLocal) {
                                alert("You must select a user before proceeding!");
                            } else {
                                setOpen(false);
                            }
                        }}
                    >
                        <Icon glyph="X" />
                    </div>
                </div>

                <div className={styles.modalMainContent}>
                    {pendingUser && (
                        <div className={styles.loginTransitionOverlay}>
                            <div className={styles.loginTransitionSpinner} />
                            <div className={styles.loginTransitionText}>Logging in as {pendingUser.name}...</div>
                        </div>
                    )}
                    <H2 className={styles.centerText}>Welcome to Leafy Bank</H2>

                    {step === 'choose' && (
                        <>
                            <Description className={styles.descriptionModal}>
                                Choose who you are to get started:
                            </Description>
                            <div className={styles.categoryContainer}>
                                <div
                                    className={`${styles.categoryCard} ${styles.categoryRetail}`}
                                    onClick={handleRetailSelect}
                                >
                                    <div className={styles.categoryEmoji}>🏦</div>
                                    <div className={styles.categoryTitle}>Bank Customer</div>
                                    <div className={styles.categoryDescription}>
                                        Pretend you're a customer of Leafy Bank — access payment, account creation and open banking demo flows.
                                    </div>
                                </div>
                                <div
                                    className={`${styles.categoryCard} ${styles.categoryBackoffice}`}
                                    onClick={() => setStep('backoffice')}
                                >
                                    <div className={styles.categoryEmoji}>🛡️</div>
                                    <div className={styles.categoryTitle}>Backoffice</div>
                                    <div className={styles.categoryDescription}>
                                        Access all bank backoffice operational features including fraud detection and portfolio management.
                                    </div>
                                </div>
                            </div>
                        </>
                    )}

                    {step === 'backoffice' && (
                        <>
                            <Description className={styles.descriptionModal}>
                                Select a backoffice user to login as:
                            </Description>
                            <div className={styles.usersContainer}>
                                {backofficeUsers.map(user => (
                                    <User
                                        user={user}
                                        isSelectedUser={selectedLocal && selectedLocal.id === user.id}
                                        key={user.id}
                                        setOpen={setOpen}
                                        setLocalSelectedUser={handleUserSelect}
                                        onBeforeSelect={showLoginTransition}
                                        onExternalOpen={() => setPendingUser(null)}
                                    />
                                ))}
                            </div>
                        </>
                    )}
                </div>
            </Container>
        </Modal>
    );
};

export default Login;
