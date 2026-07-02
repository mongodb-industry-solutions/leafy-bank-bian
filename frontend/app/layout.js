import "./globals.css";
import { Providers } from "./providers";
import AppShell from "@/components/AppShell/AppShell";


// TODO: Update metadata with actual demo details
export const metadata = {
  title: "Leafy Bank",
  description: "Industry Solutions Demo Template for NextJS",
};

export default function RootLayout({ children }) {
  // Read at runtime (server) — the BIAN explorer is a separate deployment.
  // Helm injects BIAN_MODEL_URL as container env; default to local docker-compose port.
  const bianModelUrl = process.env.BIAN_MODEL_URL || "http://localhost:8004";

  return (
    <html lang="en">
      <body>
        <Providers>
          <AppShell bianModelUrl={bianModelUrl}>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
