import type { Metadata, Viewport } from "next";
import { connection } from "next/server";

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

// Every page must render per request: Next.js adds the CSP nonce (src/proxy.ts) only to
// scripts it renders with a request. A prerendered static shell (Partial Prerendering /
// Cache Components) carries no nonce, so the browser blocks its scripts and the app never
// starts. Data is fetched client-side with TanStack Query, so nothing is lost.
export default async function RootLayout({ children }: LayoutProps<"/">) {
  await connection();
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:border focus:border-foreground focus:bg-background focus:p-2 focus:text-foreground"
        >
          Skip to main content
        </a>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
