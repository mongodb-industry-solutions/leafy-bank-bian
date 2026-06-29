import "./globals.css";
import { Providers } from "./providers";
import NavBar from "@/components/NavBar/NavBar";
import FloatingAssistant from "@/components/FloatingAssistant/FloatingAssistant";


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
          <NavBar bianModelUrl={bianModelUrl} />
          <div className="appContent">{children}</div>
          <FloatingAssistant />
        </Providers>
      </body>
    </html>
  );
}
