"use client";

import { GeistSans } from "geist/font/sans";
import LeafyGreenProvider from "@leafygreen-ui/leafygreen-provider";
import Link from "next/link";
import "./globals.css";

function Navigation() {
  return (
    <nav className="nav-header">
      <div className="nav-container">
        <Link href="/" className="nav-logo-group">
          <svg className="mongodb-leaf" width="24" height="24" viewBox="0 0 24 24" fill="none">
            <path d="M12.5 2C12.5 2 7 5.5 7 12C7 18.5 12 22 12 22C12 22 17 18.5 17 12C17 5.5 12.5 2 12.5 2Z" fill="currentColor"/>
            <path d="M12 22C12 22 11.5 20 11.5 17C11.5 14 12 12 12 12C12 12 12.5 14 12.5 17C12.5 20 12 22 12 22Z" fill="currentColor" opacity="0.5"/>
          </svg>
          <div className="nav-logo-text">
            <span className="logo-primary">BIAN Data Model Explorer</span>
            <span className="logo-secondary">Powered by MongoDB</span>
          </div>
        </Link>
      </div>
    </nav>
  );
}

export default function RootLayout({ children }) {
  return (
    <html lang="en" className={GeistSans.className}>
      <head>
        <title>BIAN Data Model Explorer</title>
        <link rel="icon" href="/leaf-icon.svg" type="image/svg+xml" />
      </head>
      <body>
        <LeafyGreenProvider>
          <Navigation />
          <main className="main-content">
            {children}
          </main>
        </LeafyGreenProvider>
      </body>
    </html>
  );
}