import type { Metadata, Viewport } from "next";
import { connection } from "next/server";
import { Suspense, type ReactNode } from "react";

import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "AlloyIQ",
  description: "AI research for metals markets. Research only; payments are simulated.",
};

// Browser UI colour (mobile address bar, installed-app title bar) matches the green theme.
export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#dcf5dc" },
    { media: "(prefers-color-scheme: dark)", color: "#0b2e1a" },
  ],
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
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:border focus:border-foreground focus:bg-background focus:p-2 focus:text-foreground"
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
