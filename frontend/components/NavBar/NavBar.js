"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import Image from "next/image";
import { usePathname, useRouter } from "next/navigation";
import { Body } from "@leafygreen-ui/typography";
import styles from "./NavBar.module.css";
import { useUser } from "@/lib/context/UserContext";
import Modal from "@leafygreen-ui/modal";
import Icon from "@leafygreen-ui/icon";

const NavBar = () => {
    const [mounted, setMounted] = useState(false);

    useEffect(() => {
        setMounted(true);
    }, []);

    // Guard for SSR — UserContext reads localStorage on mount
    if (!mounted) {
        return (
            <header className={styles.navBar}>
                <div className={styles.left}>
                    <Link href="/" className={styles.logoLink} aria-label="Leafy Bank home">
                        <Image src="/leafy_bank_logo.png" alt="Leafy Bank" width={200} height={36} />
                    </Link>
                </div>

                <nav className={styles.center} aria-label="Main navigation">
                    <Link href="/" className={styles.navLink}>
                        <Body weight="medium">My Bank</Body>
                    </Link>
                    <Link href="/portfolio" className={styles.navLink}>
                        <Body weight="medium">Asset & Crypto Portfolio</Body>
                    </Link>
                </nav>

                <div className={styles.right}>
                    <Body>User</Body>
                </div>
            </header>
        );
    }

    return <NavBarContent />;
};

const NavBarContent = () => {
    const { selectedUser, profile, setProfile, authorizedConsents, clearUser } = useUser();
    const pathname = usePathname();
    const router = useRouter();
    const userName = selectedUser?.name || "User";
    const userRole = selectedUser?.role || "";
    const userID = selectedUser?.id || "12345";
    const [showUserModal, setShowUserModal] = useState(false);

    const handleSwitchUser = (e) => {
        e.stopPropagation();
        clearUser();
        router.push('/');
    };

    return (
        <>
            <Modal open={showUserModal} setOpen={setShowUserModal} aria-label="User info">
                <div className={styles.userModalContent}>
                    {selectedUser?.id && (
                        <Image
                            src={`/users/${userID}.png`}
                            alt={userName}
                            width={150}
                            height={80}
                            className={styles.userImageModal}
                        />
                    )}
                    <Body weight="medium">{userName}</Body>
                    {userRole && <Body className={styles.userRole}>{userRole}</Body>}
                    {/* additional tags */}
                    <div className={styles.userTags}>
                        <div className={styles.tag}>Employer: {selectedUser?.employer || 'N/A'}</div>
                        <div className={styles.tag}>Type: {selectedUser?.employmentType || 'N/A'}</div>
                        <div className={styles.tag}>Job Title: {selectedUser?.jobTitle || 'N/A'}</div>
                        <div className={styles.tag}>Income: {selectedUser?.incomeAmount || 'N/A'} {selectedUser?.currency || ''} / {selectedUser?.incomeFrequency || ''}</div>
                        <div className={styles.tag}>Spending Profile: {selectedUser?.spendingProfile || 'N/A'}</div>
                    </div>
                    <button className={styles.switchUserModalBtn} onClick={handleSwitchUser}>
                        <Icon glyph="Refresh" size="small" /> Switch user
                    </button>
                </div>
            </Modal>
            <header className={styles.navBar}>
                <div className={styles.left}>
                    <Link href="/" className={styles.logoLink} aria-label="Leafy Bank home">
                        <Image src="/leafy_bank_logo.png" alt="Leafy Bank" width={200} height={36} />
                    </Link>
                </div>

                <nav className={styles.center} aria-label="Main navigation">
                    <Link href="/" className={styles.navLink}>
                        <Body weight="medium" className={pathname === "/" ? styles.navLinkActive : ""}>My Bank</Body>
                    </Link>
                    <Link href="/portfolio" className={styles.navLink}>
                        <Body weight="medium" className={pathname === "/portfolio" ? styles.navLinkActive : ""}>Asset & Crypto Portfolio</Body>
                    </Link>
                </nav>

                <div className={styles.right}>
                    {authorizedConsents.map(({ consentId, institution }) => (
                        <span key={consentId} className={styles.consentBadge}>
                            Connected to {institution}
                        </span>
                    ))}

                    {/*}
                        <div className={styles.profileContainer}>
                        <label className={styles.profileLabel}>Spending Profile</label>
                        <select
                            className={styles.profileSelect}
                            value={profile}
                            onChange={(e) => setProfile(e.target.value)}
                        >
                            <option value="balanced">Balanced</option>
                            <option value="overspender">Overspender</option>
                            <option value="saver">Saver</option>
                        </select>
                    </div>*/}

                    <div className={styles.userInfoContainer} onClick={() => setShowUserModal(true)}>
                        {selectedUser?.id && (
                            <Image
                                src={`/users/${userID}.png`}
                                alt={userName}
                                width={30}
                                height={40}
                                className={styles.userImage}
                            />
                        )}

                        <div className={styles.userDetails}>
                            <Body>{userName}</Body>
                            {userRole && <div className={styles.userRole}>{userRole}</div>}
                        </div>

                        <button
                            className={styles.switchUserBtn}
                            onClick={handleSwitchUser}
                            aria-label="Switch user"
                            title="Switch user"
                        >
                            <Icon glyph="Refresh" size="small" />
                        </button>
                    </div>

                    <button
                        type="button"
                        className={styles.mobileMenuButton}
                        onClick={() => setShowUserModal(true)}
                        aria-label="Open user menu"
                    >
                        <span className={styles.mobileMenuIcon}>☰</span>
                    </button>
                </div>
            </header>
        </>
    );
};

export default NavBar;
