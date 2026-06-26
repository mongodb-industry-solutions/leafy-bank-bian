import "../globals.css";
import { Providers } from "../providers";

export default function BankLoginLayout({ children }) {
  return (
    <html lang="en">
      <body>
        <Providers>
          <div className="appContent">{children}</div>
        </Providers>
      </body>
    </html>
  );
}