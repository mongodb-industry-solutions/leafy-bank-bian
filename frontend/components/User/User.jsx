"use client";

// User.jsx

import React from 'react';
import { Body } from '@leafygreen-ui/typography';
import Card from '@leafygreen-ui/card';
import { useUser } from '@/lib/context/UserContext';
import styles from './User.module.css';

const User = ({
    user = null,
    isSelectedUser = false,
    setOpen,
    setLocalSelectedUser = null,
    onBeforeSelect = null,
    onExternalOpen = null,
}) => {
    const { markIntentionalNavigation } = useUser();
    const handleClick = async () => {
        if (onBeforeSelect) await onBeforeSelect(user);
        if (user.url) {
            // Internal routes (relative paths) navigate in the same tab;
            // external demo links open in a new tab.
            if (user.url.startsWith('/')) {
                // Persist the selection first — the destination route brings its
                // own header (e.g. gl-pipeline-monitor's TopBar) that reads the
                // user from UserContext/localStorage after this full-page nav.
                // Flag the nav as intentional so it isn't seen as a fresh load.
                markIntentionalNavigation();
                setLocalSelectedUser?.(user);
                window.location.href = user.url;
            } else {
                // Unlike the same-tab branches above, nothing here closes the modal
                // or navigates it away — this tab stays put, so whatever onBeforeSelect
                // showed (e.g. a "Logging in..." overlay) must be dismissed explicitly
                // or it hangs on screen after the new tab opens.
                window.open(user.url, '_blank');
                onExternalOpen?.();
            }
            return;
        }
        if (!setLocalSelectedUser) return;
        setLocalSelectedUser(user);
        setOpen(false);
    };

    return (
        <Card
            className={`${styles.userCard} ${user !== null ? 'cursorPointer' : ''} ${isSelectedUser ? styles.userSelected : ''}`}
            onClick={handleClick}
        >
            <img src={`/users/${user.id}.png`} alt="User Avatar" />
            <Body className={styles.userName}>{user.name}</Body>
            <Body className={styles.userRole}>{user.role}</Body>
            {user.spendingProfile && (
                <span className={`${styles.spendingBadge} ${styles[`spending${user.spendingProfile}`]}`}>
                    {user.spendingProfile}
                </span>
            )}
            {user.features && user.features.length > 0 && (
                <>
                    <hr className={styles.featureDivider} />
                    <ul className={styles.featureList}>
                        {user.features.map((f, i) =>
                            typeof f === 'string' ? (
                                <li key={i} className={styles.featureItem}>{f}</li>
                            ) : (
                                <li key={i} className={styles.featureGroup}>
                                    <span className={styles.featureGroupName}>{f.group}</span>
                                    <ul className={styles.featureSubList}>
                                        {f.items.map((item, j) => (
                                            <li key={j} className={styles.featureItem}>{item}</li>
                                        ))}
                                    </ul>
                                </li>
                            )
                        )}
                    </ul>
                </>
            )}
        </Card>
    );
};

export default User;
