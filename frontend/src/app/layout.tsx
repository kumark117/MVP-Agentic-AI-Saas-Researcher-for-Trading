import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AlloyIQ",
  description: "AI research for metals markets. Research only; payments are simulated.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
