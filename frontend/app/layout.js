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
  return (
    <html lang="en">
      <body>
        <Providers>
          <NavBar />
          <div className="appContent">{children}</div>
          <FloatingAssistant />
        </Providers>
      </body>
    </html>
  );
}
