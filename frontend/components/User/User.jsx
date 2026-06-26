"use client";

// User.jsx

import React from 'react';
import { Body } from '@leafygreen-ui/typography';
import Card from '@leafygreen-ui/card';
import styles from './User.module.css';

const User = ({ user = null, isSelectedUser = false, setOpen, setLocalSelectedUser = null }) => {
    const handleClick = () => {
        if (user.url) {
            window.open(user.url, '_blank');
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
