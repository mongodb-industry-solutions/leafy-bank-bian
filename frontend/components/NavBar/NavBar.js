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

                <nav className={styles.center} aria-label="Main navigation" />

                <div className={styles.right} />
            </header>
        );
    }

    return <NavBarContent bianModelUrl={bianModelUrl} />;
};


const NavBarContent = ({ bianModelUrl }) => {
    const { selectedUser, authorizedConsents, loginInProgress } = useUser();
    const pathname = usePathname();
    const isGlMonitor = pathname?.startsWith("/gl-pipeline-monitor");
    // Before a user is chosen (welcome modal), show only the logo — no nav links or user controls.
    // The GL monitor runs as an implicit ops user, so it's always treated as signed in.
    // selectedUser is set as soon as the login flow starts (to prefetch data in the
    // background) — loginInProgress excludes that window so the header doesn't show
    // "Frida" while the login modal/overlay is still on screen.
    const hasUser = isGlMonitor || (!!selectedUser?.id && !loginInProgress);
    const hideNavLinks = isGlMonitor || !hasUser;

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
                {hasUser && (
                    <>
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
                    </>
                )}
            </div>
        </header>
    );
};

export default NavBar;
