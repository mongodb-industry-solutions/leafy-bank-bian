"use client";

import React, { useState } from 'react';
import Icon from '@leafygreen-ui/icon';
import { Modal, Container } from 'react-bootstrap';
import { H2, Description } from '@leafygreen-ui/typography';
import styles from './Login.module.css';
import User from '@/components/User/User';
import { USER_LIST } from "@/lib/constants";
import Banner from "@leafygreen-ui/banner";
import { useUser } from "@/lib/context/UserContext";

const Login = ({ onDone }) => {
    const { selectUser } = useUser();
    const [open, setOpen] = useState(true);
    const [selectedLocal, setSelectedLocal] = useState(null);
    const [step, setStep] = useState('choose'); // 'choose' | 'retail' | 'backoffice'

    const retailUsers = USER_LIST.filter(u => u.section === 'retail');
    const backofficeUsers = USER_LIST.filter(u => u.section === 'backoffice');

    const handleUserSelect = (user) => {
        setSelectedLocal(user);
        selectUser(user);
        setOpen(false);
        onDone?.();
    };

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
                    <H2 className={styles.centerText}>Welcome to Leafy Bank</H2>

                    {step === 'choose' && (
                        <>
                            <Description className={styles.descriptionModal}>
                                Choose who you are to get started:
                            </Description>
                            <div className={styles.categoryContainer}>
                                <div
                                    className={`${styles.categoryCard} ${styles.categoryRetail}`}
                                    onClick={() => setStep('retail')}
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

                    {step === 'retail' && (
                        <>
                            <Description className={styles.descriptionModal}>
                                Select a bank customer to login as:
                            </Description>
                            <div className={styles.usersContainer}>
                                {retailUsers.map(user => (
                                    <User
                                        user={user}
                                        isSelectedUser={selectedLocal && selectedLocal.id === user.id}
                                        key={user.id}
                                        setOpen={setOpen}
                                        setLocalSelectedUser={handleUserSelect}
                                    />
                                ))}
                            </div>
                            <Banner variant="warning" className={styles.warningBanner}>
                                Please make sure pop-ups are enabled in your browser to ensure the demo runs smoothly and all features display correctly.
                            </Banner>
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
