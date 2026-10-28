import type { Metadata } from "next";
import { connection } from "next/server";
import { Suspense, type ReactNode } from "react";

import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "AlloyIQ",
  description: "AI research for metals markets. Research only; payments are simulated.",
};

// The CSP nonce (src/proxy.ts) is applied during dynamic rendering, so every request
// renders on the server; the app's data is fetched client-side with TanStack Query.
async function Dynamic({ children }: { children: ReactNode }) {
  await connection();
  return <Providers>{children}</Providers>;
}

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:bg-white focus:p-2 focus:text-black"
        >
          Skip to main content
        </a>
        <Suspense fallback={null}>
          <Dynamic>{children}</Dynamic>
        </Suspense>
      </body>
    </html>
  );
}
