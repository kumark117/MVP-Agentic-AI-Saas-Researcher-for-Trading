"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { Announcer } from "@/components/Announcer";

let browserQueryClient: QueryClient | undefined;

function getQueryClient() {
  // Isolated per server render; one shared client in the browser.
  if (typeof window === "undefined") return new QueryClient();
  browserQueryClient ??= new QueryClient({
    defaultOptions: { queries: { refetchOnWindowFocus: true, staleTime: 5_000 } },
  });
  return browserQueryClient;
}

export function Providers({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={getQueryClient()}>
      <Announcer>{children}</Announcer>
    </QueryClientProvider>
  );
}
