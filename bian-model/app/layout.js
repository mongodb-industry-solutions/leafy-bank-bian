"use client";

import { GeistSans } from "geist/font/sans";
import LeafyGreenProvider from "@leafygreen-ui/leafygreen-provider";
import "./globals.css";

export default function RootLayout({ children }) {
  return (
    <html lang="en" className={GeistSans.className}>
      <head>
        <title>BIAN Data Model Explorer</title>
        <link rel="icon" href="/leaf-icon.svg" type="image/svg+xml" />
      </head>
      <body>
        <LeafyGreenProvider>
          <main className="main-content">
            {children}
          </main>
        </LeafyGreenProvider>
      </body>
    </html>
  );
}