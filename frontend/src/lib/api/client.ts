/**
 * Typed API client generated from the committed OpenAPI contract (D110).
 *
 * - Same-origin only; the session cookie is HttpOnly and never touched by JS.
 * - Mutations send the CSRF token (double-submit, ADR-012). On 403 CSRF_FAILED the
 *   client refetches the token and retries once: the server rejected the request before
 *   doing anything, so a retry cannot duplicate an action.
 * - Errors are application/problem+json and surface as ApiError with the stable code.
 */
import createClient, { type Middleware } from "openapi-fetch";

import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Problem = Schemas["Problem"];

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly problem: Partial<Problem>;
  readonly retryAfter: number | null;

  constructor(status: number, problem: Partial<Problem>, retryAfter: number | null = null) {
    super(problem.detail ?? problem.title ?? `Request failed (${status})`);
    this.status = status;
    this.code = problem.code ?? "INTERNAL_ERROR";
    this.problem = problem;
    this.retryAfter = retryAfter;
  }
}

let csrfToken: string | null = null;

export async function refreshCsrf(): Promise<string> {
  const r = await fetch("/api/v1/auth/csrf", { credentials: "same-origin" });
  const body = (await r.json()) as { data: { csrf_token: string } };
  csrfToken = body.data.csrf_token;
  return csrfToken;
}

/** Login, logout and password change return a new token bound to the new session. */
export function setCsrf(token: string | null | undefined): void {
  if (token) csrfToken = token;
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

const csrf: Middleware = {
  async onRequest({ request }) {
    if (!UNSAFE.has(request.method)) return request;
    const token = csrfToken ?? (await refreshCsrf());
    request.headers.set("X-CSRF-Token", token);
    return request;
  },
};

export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });
api.use(csrf);

type Result<T> = { data?: { data: T }; error?: unknown; response: Response };

/** Unwrap {data, meta}; throw ApiError for problem+json. Retries once on CSRF_FAILED. */
export async function call<T>(fn: () => Promise<Result<T>>): Promise<T> {
  let res = await fn();
  if (res.response.status === 403 && (res.error as Problem | undefined)?.code === "CSRF_FAILED") {
    await refreshCsrf();
    res = await fn();
  }
  if (res.error !== undefined || !res.data) {
    const retry = res.response.headers.get("Retry-After");
    throw new ApiError(
      res.response.status,
      (res.error ?? {}) as Partial<Problem>,
      retry ? Number(retry) : null,
    );
  }
  const body = res.data.data as T & { csrf_token?: string };
  if (body && typeof body === "object" && "csrf_token" in body) setCsrf(body.csrf_token);
  return body;
}

/** A fresh Idempotency-Key per user intent, reused across retries of that intent. */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
