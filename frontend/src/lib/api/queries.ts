"use client";

/**
 * TanStack Query hooks over the typed client. Persisted snapshots are authoritative;
 * SSE events only trigger invalidation of these keys (D055).
 */
import { useQuery } from "@tanstack/react-query";

import { api, ApiError, call, type Schemas } from "./client";

export const keys = {
  me: ["me"] as const,
  balance: ["balance"] as const,
  ledger: (cursor?: string) => ["ledger", cursor ?? ""] as const,
  receipts: ["receipts"] as const,
  instruments: ["catalog", "instruments"] as const,
  models: ["catalog", "models"] as const,
  plans: ["catalog", "plans"] as const,
  runs: (filters: Record<string, string | undefined> = {}) => ["runs", filters] as const,
  run: (id: string) => ["run", id] as const,
  report: (id: string) => ["run", id, "report"] as const,
  usage: (id: string) => ["run", id, "usage"] as const,
  activity: (id: string) => ["run", id, "activity"] as const,
  sessions: ["sessions"] as const,
  checkout: (id: string) => ["checkout", id] as const,
};

const noRetryOnAuth = (failureCount: number, error: unknown) =>
  !(error instanceof ApiError && [401, 403, 404, 409].includes(error.status)) && failureCount < 2;

export function useMe() {
  return useQuery({
    queryKey: keys.me,
    queryFn: () => call<Schemas["Me"]>(() => api.GET("/api/v1/me")),
    retry: noRetryOnAuth,
    staleTime: 60_000,
  });
}

export function useBalance() {
  return useQuery({
    queryKey: keys.balance,
    queryFn: () => call<Schemas["Balance"]>(() => api.GET("/api/v1/account/balance")),
    retry: noRetryOnAuth,
  });
}

export function useInstruments() {
  return useQuery({
    queryKey: keys.instruments,
    queryFn: () => call<Schemas["Catalog"]>(() => api.GET("/api/v1/catalog/instruments")),
    staleTime: 5 * 60_000,
  });
}

export function useModels() {
  return useQuery({
    queryKey: keys.models,
    queryFn: () => call<Schemas["ModelProfile"][]>(() => api.GET("/api/v1/catalog/models")),
    staleTime: 5 * 60_000,
  });
}

export function usePlans() {
  return useQuery({
    queryKey: keys.plans,
    queryFn: () => call<Schemas["Plan"][]>(() => api.GET("/api/v1/catalog/plans")),
    staleTime: 5 * 60_000,
  });
}

export function useRuns(filters: Record<string, string | undefined> = {}, limit = 10) {
  return useQuery({
    queryKey: keys.runs({ ...filters, limit: String(limit) }),
    queryFn: () =>
      call<Schemas["Page_RunSummary_"]>(() =>
        api.GET("/api/v1/research/runs", {
          params: { query: { limit, ...(filters as Record<string, never>) } },
        }),
      ),
    retry: noRetryOnAuth,
  });
}

export function useRun(id: string) {
  return useQuery({
    queryKey: keys.run(id),
    queryFn: () =>
      call<Schemas["RunSnapshot"]>(() =>
        api.GET("/api/v1/research/runs/{run_id}", { params: { path: { run_id: id } } }),
      ),
    retry: noRetryOnAuth,
  });
}

export function useReport(id: string, enabled: boolean) {
  return useQuery({
    queryKey: keys.report(id),
    enabled,
    queryFn: () =>
      call<Schemas["Report"]>(() =>
        api.GET("/api/v1/research/runs/{run_id}/report", { params: { path: { run_id: id } } }),
      ),
    retry: noRetryOnAuth,
  });
}

export function useUsage(id: string) {
  return useQuery({
    queryKey: keys.usage(id),
    queryFn: () =>
      call<Schemas["RunUsage"]>(() =>
        api.GET("/api/v1/research/runs/{run_id}/usage", { params: { path: { run_id: id } } }),
      ),
    retry: noRetryOnAuth,
  });
}

export function useActivity(id: string) {
  return useQuery({
    queryKey: keys.activity(id),
    queryFn: () =>
      call<Schemas["Page_Activity_"]>(() =>
        api.GET("/api/v1/research/runs/{run_id}/activity", {
          params: { path: { run_id: id }, query: { limit: 500 } },
        }),
      ),
    retry: noRetryOnAuth,
  });
}

export function useSessions() {
  return useQuery({
    queryKey: keys.sessions,
    queryFn: () => call<Schemas["Page_SessionInfo_"]>(() => api.GET("/api/v1/me/sessions")),
    retry: noRetryOnAuth,
  });
}
