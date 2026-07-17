"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import Image from "next/image";
import { usePathname } from "next/navigation";
import { Body } from "@leafygreen-ui/typography";
import styles from "./NavBar.module.css";
import { useUser } from "@/lib/context/UserContext";
import Icon from "@leafygreen-ui/icon";
import UserInfo from "./UserInfo";

const NavBar = ({ bianModelUrl }) => {
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
                        <Body weight="medium">Personal Banking</Body>
                    </Link>
                    <Link href="/portfolio" className={styles.navLink}>
                        <Body weight="medium">Investment Accounts</Body>
                    </Link>
                </nav>

                <div className={styles.right}>
                    <Body>User</Body>
                </div>
            </header>
        );
    }

    return <NavBarContent bianModelUrl={bianModelUrl} />;
};


const NavBarContent = ({ bianModelUrl }) => {
    const { authorizedConsents } = useUser();
    const pathname = usePathname();
    const hideNavLinks = pathname?.startsWith("/gl-pipeline-monitor");

    return (
        <header className={styles.navBar}>
            <div className={styles.left}>
                <Link href="/" className={styles.logoLink} aria-label="Leafy Bank home">
                    <Image src="/leafy_bank_logo.png" alt="Leafy Bank" width={200} height={36} />
                </Link>
            </div>

            <nav className={styles.center} aria-label="Main navigation">
                {!hideNavLinks && (
                    <>
                        <Link href="/" className={styles.navLink}>
                            <Body weight="medium" className={pathname === "/" ? styles.navLinkActive : ""}>Personal Banking</Body>
                        </Link>
                        <Link href="/portfolio" className={styles.navLink}>
                            <Body weight="medium" className={pathname === "/portfolio" ? styles.navLinkActive : ""}>Investment Accounts</Body>
                        </Link>
                    </>
                )}
            </nav>

            <div className={styles.right}>
                {authorizedConsents.map(({ consentId, institution }) => (
                    <span key={consentId} className={styles.consentBadge}>
                        Connected to {institution}
                    </span>
                ))}

                {bianModelUrl && (
                    <a
                        href={`${bianModelUrl}/bian-data-model?demo=leafy-bank`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className={styles.bianModelLink}
                        title="Explore the BIAN-aligned data model"
                    >
                        <Icon glyph="Visibility" size="small" />
                        <Body weight="medium">View Data Model</Body>
                    </a>
                )}

                <UserInfo />
            </div>
        </header>
    );
};

export default NavBar;
