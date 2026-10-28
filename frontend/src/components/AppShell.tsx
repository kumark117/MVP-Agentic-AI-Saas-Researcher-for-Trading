"use client";

/**
 * Signed-in shell: navigation, session guard, simulation banner and the account stream.
 * The account stream (GET /api/v1/events) keeps every device in step: run, balance and
 * session events invalidate the matching snapshots (D055).
 */
import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { DemoMoney, SimulationBanner } from "@/components/ui";
import { api, ApiError, call } from "@/lib/api/client";
import { keys, useBalance, useMe } from "@/lib/api/queries";
import { ACCOUNT_EVENT_TYPES, StreamController } from "@/lib/sse/StreamController";

const NAV = [
  { href: "/research", label: "Research" },
  { href: "/history", label: "History" },
  { href: "/reports", label: "Reports" },
  { href: "/account", label: "Account" },
  { href: "/security", label: "Security" },
];

function useAccountStream(enabled: boolean, currentSession: string | undefined) {
  const qc = useQueryClient();
  const router = useRouter();
  useEffect(() => {
    if (!enabled) return;
    const c = new StreamController({
      url: "/api/v1/events",
      eventTypes: ACCOUNT_EVENT_TYPES,
      onEvent: (e) => {
        if (e.type === "balance.updated") void qc.invalidateQueries({ queryKey: keys.balance });
        if (e.type === "run.updated") {
          void qc.invalidateQueries({ queryKey: ["runs"] });
          const id = String(e.payload.run_id ?? "");
          if (id) void qc.invalidateQueries({ queryKey: keys.run(id) });
        }
        if (e.type === "security.session_revoked") {
          void qc.invalidateQueries({ queryKey: keys.sessions });
          if (e.payload.session_id === currentSession) router.push("/login");
        }
      },
      onStatus: (s) => {
        if (s === "unauthenticated") router.push("/login");
      },
      probe: async () => {
        try {
          await call(() => api.GET("/api/v1/me"));
          return { kind: "ok", latestEventId: null };
        } catch (err) {
          return err instanceof ApiError && err.status === 401
            ? { kind: "unauthenticated" }
            : { kind: "gone" };
        }
      },
    });
    c.start();
    return () => c.stop();
  }, [enabled, currentSession, qc, router]);
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const me = useMe();
  const balance = useBalance();
  const unauth = me.error instanceof ApiError && me.error.status === 401;

  useEffect(() => {
    if (unauth) router.replace("/login");
  }, [unauth, router]);
  useAccountStream(!!me.data, me.data?.current_session_id);

  if (me.isPending || unauth) {
    return (
      <main id="main" className="p-8">
        <p role="status">Loading your account...</p>
      </main>
    );
  }

  async function logout() {
    await call(() => api.POST("/api/v1/auth/logout")).catch(() => undefined);
    router.push("/login");
  }

  return (
    <div className="flex min-h-full flex-col">
      <SimulationBanner />
      <header className="border-b border-gray-300 px-4 py-3 dark:border-gray-700">
        <nav aria-label="Main" className="mx-auto flex max-w-6xl flex-wrap items-center gap-4">
          <Link href="/research" className="font-semibold">
            AlloyIQ
          </Link>
          <ul className="flex flex-wrap gap-3">
            {NAV.map((n) => (
              <li key={n.href}>
                <Link
                  href={n.href}
                  aria-current={pathname.startsWith(n.href) ? "page" : undefined}
                  className={`rounded px-2 py-1 underline-offset-4 hover:underline ${pathname.startsWith(n.href) ? "font-semibold underline" : ""}`}
                >
                  {n.label}
                </Link>
              </li>
            ))}
            <li>
              <a
                href="/about"
                target="_blank"
                rel="noopener noreferrer"
                className="px-2 py-1 hover:underline"
              >
                About (new tab)
              </a>
            </li>
          </ul>
          <span className="ml-auto text-sm">
            Demo balance: <DemoMoney value={balance.data?.available} />
          </span>
          <button type="button" onClick={logout} className="text-sm underline">
            Sign out
          </button>
        </nav>
      </header>
      <main id="main" className="mx-auto w-full max-w-6xl flex-1 px-4 py-6">
        {children}
      </main>
      <footer className="border-t border-gray-300 px-4 py-3 text-xs dark:border-gray-700">
        Research only, not investment advice. Reference data, not a live exchange feed. Money
        collected: ₹0.00.
      </footer>
    </div>
  );
}
