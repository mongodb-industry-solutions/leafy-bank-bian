"use client";

import React, { useState, useRef, useEffect } from "react";
import Image from "next/image";
import { usePathname, useRouter } from "next/navigation";
import { Body } from "@leafygreen-ui/typography";
import Modal from "@leafygreen-ui/modal";
import Icon from "@leafygreen-ui/icon";
import { useUser } from "@/lib/context/UserContext";
import { USER_LIST } from "@/lib/constants";
import styles from "./NavBar.module.css";

// Users grouped by section for the switch-user dropdown, in display order.
const SECTION_ORDER = ["retail", "backoffice"];
const SECTION_LABELS = { retail: "Bank Customers", backoffice: "Backoffice" };
const USERS_BY_SECTION = SECTION_ORDER
    .map((section) => ({
        section,
        label: SECTION_LABELS[section] || section,
        users: USER_LIST.filter((u) => u.section === section),
    }))
    .filter((group) => group.users.length > 0);

/**
 * User section of the NavBar: the avatar/details container, the mobile menu
 * button, and the modal they open. Self-contained — derives the displayed
 * user (real user, or the "Marc" persona on the GL Pipeline Monitor route)
 * and owns its own modal open state and switch-user handler.
 *
 * The switch-user button opens a dropdown of all users (grouped by section);
 * selecting one routes to that user's point of view, and a Logout entry clears
 * the session so the Login screen reappears.
 */
const UserInfo = () => {
    const { selectedUser, selectUser, clearUser } = useUser();
    const pathname = usePathname();
    const router = useRouter();
    const [showUserModal, setShowUserModal] = useState(false);
    const [showUserDropdown, setShowUserDropdown] = useState(false);
    const dropdownWrapperRef = useRef(null);

    const isGlMonitor = pathname?.startsWith("/gl-pipeline-monitor");
    const userName = isGlMonitor ? "Marc" : (selectedUser?.name || "User");
    const userRole = isGlMonitor ? "Finance Operator" : (selectedUser?.role || "");
    const userID = selectedUser?.id || "12345";
    const userAvatar = isGlMonitor ? "/user_avatar.png" : `/users/${userID}.png`;

    // Close the dropdown when clicking outside of it.
    useEffect(() => {
        if (!showUserDropdown) return;
        const handleClickOutside = (e) => {
            if (dropdownWrapperRef.current && !dropdownWrapperRef.current.contains(e.target)) {
                setShowUserDropdown(false);
            }
        };
        document.addEventListener("mousedown", handleClickOutside);
        return () => document.removeEventListener("mousedown", handleClickOutside);
    }, [showUserDropdown]);

    // Switch to another user's point of view. Mirrors the Login flow: internal
    // routes persist the selection then full-page navigate; external demo links
    // open in a new tab; plain retail users just switch and return home.
    const handleSelectUser = (user) => {
        setShowUserDropdown(false);
        if (user.url) {
            if (user.url.startsWith("/")) {
                selectUser(user);
                window.location.href = user.url;
            } else {
                window.open(user.url, "_blank");
            }
            return;
        }
        selectUser(user);
        router.push("/");
    };

    // Logout: clear the session and return home, where Login is shown again.
    const handleLogout = () => {
        setShowUserDropdown(false);
        setShowUserModal(false);
        clearUser();
        router.push("/");
    };

    return (
        <>
            <Modal open={showUserModal} setOpen={setShowUserModal} aria-label="User info">
                <div className={styles.userModalContent}>
                    {(isGlMonitor || selectedUser?.id) && (
                        <Image
                            src={userAvatar}
                            alt={userName}
                            width={150}
                            height={80}
                            className={styles.userImageModal}
                        />
                    )}
                    <Body weight="medium">{userName}</Body>
                    {userRole && <Body className={styles.userRole}>{userRole}</Body>}
                    <div className={styles.userTags}>
                        <div className={styles.tag}>Employer: {isGlMonitor ? 'Leafy Bank' : (selectedUser?.employer || 'N/A')}</div>
                        <div className={styles.tag}>Type: {isGlMonitor ? 'Full-time' : (selectedUser?.employmentType || 'N/A')}</div>
                        <div className={styles.tag}>Job Title: {isGlMonitor ? 'Bank Operations Administrator' : (selectedUser?.jobTitle || 'N/A')}</div>
                        <div className={styles.tag}>Access Level: {isGlMonitor ? 'Back Office' : (selectedUser?.spendingProfile || 'N/A')}</div>
                    </div>
                    <button className={styles.switchUserModalBtn} onClick={handleLogout}>
                        <Icon glyph="Refresh" size="small" /> Switch user
                    </button>
                </div>
            </Modal>

            <div className={styles.userMenuWrapper} ref={dropdownWrapperRef}>
                <div className={styles.userInfoContainer} onClick={() => setShowUserModal(true)}>
                    {(isGlMonitor || selectedUser?.id) && (
                        <Image
                            src={userAvatar}
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
                        onClick={(e) => {
                            e.stopPropagation();
                            setShowUserDropdown((prev) => !prev);
                        }}
                        aria-label="Switch user"
                        aria-haspopup="menu"
                        aria-expanded={showUserDropdown}
                        title="Switch user"
                    >
                        <Icon glyph="Refresh" size="small" />
                    </button>
                </div>

                {showUserDropdown && (
                    <div className={styles.userDropdown} role="menu">
                        {USERS_BY_SECTION.map(({ section, label, users }) => (
                            <div key={section} className={styles.userDropdownSection}>
                                <div className={styles.userDropdownSectionLabel}>{label}</div>
                                {users.map((user) => (
                                    <button
                                        key={user.id}
                                        type="button"
                                        role="menuitem"
                                        className={`${styles.userDropdownItem} ${user.id === selectedUser?.id ? styles.userDropdownItemActive : ""}`}
                                        onClick={() => handleSelectUser(user)}
                                    >
                                        <Image
                                            src={`/users/${user.id}.png`}
                                            alt={user.name}
                                            width={32}
                                            height={32}
                                            className={styles.userDropdownAvatar}
                                        />
                                        <div className={styles.userDropdownInfo}>
                                            <Body weight="medium">{user.name}</Body>
                                            <span className={styles.userDropdownRole}>{user.role}</span>
                                        </div>
                                    </button>
                                ))}
                            </div>
                        ))}

                        <button
                            type="button"
                            className={styles.userDropdownLogout}
                            onClick={handleLogout}
                        >
                            <Icon glyph="LogOut" size="small" /> Logout
                        </button>
                    </div>
                )}
            </div>

            <button
                type="button"
                className={styles.mobileMenuButton}
                onClick={() => setShowUserModal(true)}
                aria-label="Open user menu"
            >
                <span className={styles.mobileMenuIcon}>☰</span>
            </button>
        </>
    );
};

export default UserInfo;
