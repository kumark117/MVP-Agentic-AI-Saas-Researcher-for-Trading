# Decisions (ADRs) and open decisions

Each ADR records a decision, why it was made, and the spec items it touches.
Status values: **accepted**, **superseded**, **proposed** (needs owner sign-off).

---

## ADR-001 Technology stack (accepted, P0)

Fixed by build prompt §4; recorded here so later changes are deliberate.

| Concern | Decision |
|---|---|
| Frontend | Next.js App Router + TypeScript strict, Tailwind CSS, TanStack Query, openapi-typescript + openapi-fetch, Zod, Vitest, Playwright |
| API | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 async + asyncpg, Alembic |
| Workers | Separate Python process from the same package, asyncio, jobs claimed from PostgreSQL |
| Workflow | LangGraph with the Postgres checkpointer, one graph thread per run (thread_id = run_id) |
| LLM | Claude only, through the official `anthropic` SDK behind our `LLMGateway`; LangChain only where it adds value |
| MCP | Official MCP Python SDK (FastMCP), Streamable HTTP at private `/mcp` with a service token |
| Database | PostgreSQL 16, the single source of truth |
| Cache | Redis 7, rate limits and short-lived caches only (ADR-005) |
| Email (dev) | Mailpit |
| Artifacts | `ArtifactStore` interface; MVP uses a local volume with sha256 and ownership in Postgres |
| Gateway | Caddy on `http://localhost:8080` |
| Observability | structlog JSON, LangSmith (redacted), OpenTelemetry API hooks |
| Tooling | uv, ruff, mypy (strict on `domain/` and `application/`), pytest, pytest-asyncio, Hypothesis; pnpm, ESLint, Prettier |

Versions resolved at P0 (latest stable on 2026-10-28; lockfiles committed):
Python 3.12.16, FastAPI 0.162.0, Pydantic 2.14.1, uvicorn 0.54.0, structlog 26.1.0,
asyncpg 0.32.0, pytest 9.2.0, mypy 2.4.0, ruff 0.17.3; Node 24.22.1 LTS, pnpm 12.15.0,
Next.js 16.4.3, React 19.3.0, Tailwind 4.3.3, TypeScript 5.9.3, Vitest 5.0.5, Prettier 3.9.10.
Later phases add their own dependencies when they need them (SQLAlchemy/Alembic in P1, etc.).

## ADR-002 Runtime topology (accepted, P0)

Build prompt §6. One compose project, `alloyiq`:

- `caddy` is the only port the browser uses (8080). `/api/*` goes to `api:8000` with response
  buffering off (`flush_interval -1`) for SSE; everything else goes to `web:3000`.
- `/api/v1/health/ready` is internal: Caddy answers 404 for it. Compose healthchecks call it on
  the container directly.
- `mcp` (8100) is not routed by Caddy; only the worker reaches it.
- `api`, `worker` and `mcp` share one backend image with different commands.
- `postgres`, `redis` and the Mailpit UI are published on `127.0.0.1` only, for host-run tests
  and tools. Nothing except Caddy listens on all interfaces.

## ADR-003 Session cookie naming per environment (accepted, P0)

Build prompt §6. In `APP_ENV=prod` the cookie is `__Host-alloyiq_sid` with `Secure`. In dev
over `http://localhost` browsers reject both `__Host-` cookies and `Secure` cookies, so the
cookie is `alloyiq_sid` without `Secure`. Every other attribute is identical in all
environments: `HttpOnly`, `SameSite=Lax`, `Path=/`, host-only (no `Domain`). Implemented in P2.

## ADR-004 Spec location (accepted, P0)

Build prompt §1 names `C:\Users\kumar\Downloads\kk-saas1\`. On the build machine the files
were in `C:\Users\LENOVO\Downloads\ai\`, with prefixes such as `!     ` in their names. They
are copied into `docs/specs/` under clean names, together with the build prompt itself, and
`scripts/extract-specs.ps1` regenerates the `.txt` extracts. The copies in `docs/specs/` are
authoritative for this repo.

## ADR-005 Redis holds nothing durable (accepted, P0)

Redis runs without persistence (`--save "" --appendonly no`), which makes "losing Redis loses
nothing" true by construction. Job and event wake-ups use PostgreSQL LISTEN/NOTIFY plus
polling. Rate limiting falls back to a conservative in-process limiter if Redis is down (P2).

## ADR-006 Yahoo Finance is dev-only reference data (accepted, P0; licensing open)

Build prompt §11. `yfinance` is an unofficial, unlicensed source. It is gated behind
`ENABLE_YAHOO_REFERENCE=1`, every instrument it backs is `data_mode=reference` with a
"not a live exchange quote" badge, and it is a pre-launch blocker (open decision OD-1).

## ADR-007 Database URLs are composed from per-role passwords (accepted, P0)

Build prompt §20 lists `DATABASE_URL`, `DATABASE_WORKER_URL` and `DATABASE_MIGRATOR_URL`.
Postgres also needs the same passwords to create the roles. To keep each secret in exactly
one place, `.env` holds `ALLOYIQ_{API,WORKER,MIGRATOR}_DB_PASSWORD`, and `docker-compose.yml`
builds the three URLs from them. `scripts/test.ps1` builds `TEST_DATABASE_URL` the same way
for host-run tests. The application still reads `DATABASE_URL` and friends, as the prompt
specifies. `scripts/dev.ps1` generates every `change-me` secret on first run.

Roles (created by `docker/postgres/init/01-roles.sh` on first volume init):
`alloyiq_migrator` owns the databases and, from P1, all tables; `alloyiq_api` and
`alloyiq_worker` are `NOSUPERUSER NOBYPASSRLS` and own nothing. Databases: `alloyiq` and
`alloyiq_test`.

## ADR-008 Optional host-run Next.js dev server (accepted, P0)

The build prompt requires everything to run with `docker compose up`, and it does: `web` runs
`next dev` in a container. The Next.js 16 docs bundled with the package
(`local-development.md`) warn that on Docker Desktop for Windows, bind-mounted sources make
Fast Refresh slow or unreliable, and they advise against polling as a fix. So:

- `docker compose up` / `scripts\dev.ps1` runs everything in containers (the acceptance path).
- `scripts\dev.ps1 -HostWeb` runs the backend services in containers and `pnpm dev` on
  Windows, and Caddy proxies to `host.docker.internal:3000` (set through `WEB_UPSTREAM`).

The browser still uses only `http://localhost:8080` in both modes.

## ADR-009 No third-party web fonts (accepted, P0)

The scaffold's `next/font/google` (Geist) was removed in favour of a system font stack. This
avoids a Google fetch at build and dev time and keeps the strict CSP planned for P7 simple.

## ADR-010 API shell conventions (accepted, P0)

- Success bodies are `Envelope[T] = {data, meta:{request_id, server_time}}`; errors are
  `application/problem+json` with the D109 stable codes (`alloyiq.api.errors.ErrorCode`).
  Unhandled exceptions return `INTERNAL_ERROR` with no exception text.
- Request correlation is pure ASGI middleware (not `BaseHTTPMiddleware`), so SSE responses are
  never buffered. An incoming `X-Request-ID` is accepted only if it is a UUID (D066);
  otherwise a new one is generated. The ID is echoed in the response header and bound to
  every log line.
- `/health/ready` returns 503 `PROVIDER_UNAVAILABLE` with `Retry-After: 5` when PostgreSQL
  cannot run `SELECT 1` within 2 s. D109 has no dedicated database code;
  `PROVIDER_UNAVAILABLE` is the closest "dependency unavailable" code.
- Interactive OpenAPI docs are served at `/api/v1/docs` outside prod only. The committed
  `docs/contracts/openapi.json` (P1) is the contract.

## ADR-011 Schema, RLS and system scope (accepted, P1)

- **Schema source:** `backend/src/alloyiq/repositories/schema.py` (SQLAlchemy Core). Migration
  `0001` was autogenerated from it once and is now frozen; `0002` is hand-written SQL (RLS,
  grants, triggers). `tests/integration/test_schema.py` fails on any drift between the migrated
  database and the metadata. Status columns are `TEXT` plus `CHECK` constraints rather than
  PostgreSQL enums, so adding a state is a one-line migration.
- **Retention classes** live in every table comment (`retention=<class>; ...`):
  `identity`, `security_12m`, `auth_short`, `config`, `product_12m`, `activity_90d`, `sse_30d`,
  `ephemeral_7d`, `financial_policy` (OD-6), `platform`. A test enforces it.
- **RLS:** FORCE RLS on every table with `account_id` or `user_id`, plus `accounts`, `users`
  and `real_budget_accounts`. Policies read `app.account_id` / `app.user_id`, which the unit of
  work (`repositories/db.py`) sets with `set_config(..., true)` (SET LOCAL) on every
  transaction. They reset at commit, so pooled connections cannot leak context (tested).
- **System scope:** account policies are bypassed only by `alloyiq_migrator` (migrations, the
  `alloyiq-admin` CLI, and the SECURITY DEFINER auth lookups added in P2), or by
  `alloyiq_worker` inside a transaction that sets `app.system = 'on'` (schedulers, sweepers).
  The API role cannot enter system scope even if it sets the flag (tested).
- **Grants:** the API role has no DELETE anywhere. Neither application role can UPDATE
  journals, postings, messages, reservation events, audit and security events or SSE events,
  and triggers block those changes even for the owner. Config tables are read-only to both.
- **Ledger integrity in the database:** a deferred constraint trigger requires at least 2
  postings per transaction, a zero sum per currency, and posting currency equal to the
  transaction currency. `business_event_id` is unique. A `CHECK` keeps wallet available
  balance at zero or above.
- **Rate cards:** active cards are immutable except retirement; items change only in draft.
- **Unavailable catalogue combinations** (MCX, LME, zinc, uranium) are computed from
  `domain/catalog.py` rather than stored as placeholder instrument rows with fake units.
- **Contracts:** P1 defines boundary models for every MVP route and event. `openapi.json`
  contains the implemented routes, and every boundary model under `components.schemas`, so
  the generated TypeScript types exist before each route ships. Event payloads have one JSON
  Schema per event type.

## ADR-012 Identity implementation details (accepted, P2)

- **Pre-auth lookups** (find a user by email, resolve a session by token hash, find a
  verification or reset token) use three `SECURITY DEFINER` functions owned by the migrator
  (migration 0003) with a pinned `search_path`. RLS stays FORCE on `users`, `sessions` and
  `auth_tokens`; the API role cannot scan them.
- **CSRF:** signed double-submit. `GET /auth/csrf` returns `nonce.HMAC(secret, nonce|binding)`
  and sets the same value in an HttpOnly cookie; mutations must send it in `X-CSRF-Token`
  *and* carry an `Origin` (or `Referer`) equal to `PUBLIC_ORIGIN`. The binding is the session
  ID, or `anon` before login, so login, password change and logout each issue a new token in
  the response. When a session is revoked, its token stops working too. The frontend client
  therefore refetches `/auth/csrf` and retries **once** on `403 CSRF_FAILED`, which is safe
  because the server rejects the request before doing anything.
- **One-time links** put the token in the URL *fragment* (`/verify-email#token=...`), which is
  never sent to servers, proxies or `Referer` headers (R013: no secrets in page URLs). The
  page posts it to the API.
- **Unverified accounts cannot log in** (403 `AUTH_REQUIRED`, problem type `email-unverified`).
  The check runs only after the password verifies, so it reveals nothing to someone without
  the password. Completing a password reset marks the email as verified, because it proves
  control of the mailbox.
- **Enumeration:** register, resend and forgot always return the same 202 body and do an
  Argon2 hash or verify either way; a registration attempt on an existing address emails the
  owner instead. Login failures are identical for unknown emails and wrong passwords.
- **Freshness:** login grants 5 minutes of freshness; `POST /auth/reauthenticate` renews it.
  `logout-all`, password change and deletion requests require it, and otherwise return 403
  with problem type `reauthentication-required`.
- **Deletion requests** are scheduled 7 days ahead (a grace period inside the 30-day R054
  window) and are idempotent; the purge job arrives in P9.
- **Email** is written to `email_outbox` in the same transaction as the token, and the worker
  delivers it through SMTP (Mailpit in dev), then wipes the payload holding the link.
- **Rate limits:** a Redis token bucket per IP and per subject. If Redis fails, a half-capacity
  in-process limiter takes over (never fail open).
- `Cache-Control: no-store` is on every API response.

## ADR-013 Providers, egress and MCP tools (accepted, P3)

- **Egress:** every adapter except yfinance uses `SafeHttpClient`. It allows HTTPS to an
  exact host allowlist only (Alpha Vantage, Tavily, Anthropic, LangSmith), with no IP
  literals and no credentials in URLs. Every resolved address must be public, which blocks
  private, loopback, link-local, CGNAT, metadata, multicast and documentation ranges.
  Redirects are re-validated hop by hop (max 3) and bodies are capped at 2 MB. There is a
  small window between the DNS check and the connection that DNS rebinding could exploit;
  a production deployment should add an egress proxy (OD-5).
- **yfinance** has its own HTTP stack (curl_cffi), so its traffic is *not* routed through
  `SafeHttpClient`. It is dev-only (ADR-006) and runs only with `ENABLE_YAHOO_REFERENCE=1`.
  Only the symbols GC=F, SI=F and HG=F are accepted, and the symbol comes from
  `provider_mappings`, never from model or browser input. This is another reason it is a
  pre-launch blocker (OD-1).
- **Tools** take catalogue instrument IDs only, plus an optional `focus` string limited to
  letters and punctuation. The news query is built on the server. Each tool has an 8 s
  deadline and at most two jittered retries for transient read errors. Provider output is
  validated (unit and currency must match the instrument, prices within plausible bounds,
  no future timestamps, OHLC consistency) before a `ToolEnvelope` with a SHA-256
  provenance hash is built. Failures return a typed error and `data: null`.
- **Indicators** are computed on the server by `alloyiq.indicators.core`, using
  conventions documented in the module, from validated bars. Indicators that lack enough
  history are listed under `missing` with the reason, never filled in.
- **MCP:** official SDK 2.3 `MCPServer`, stateless Streamable HTTP with JSON responses at
  `/mcp`, and DNS-rebinding protection limited to `mcp:8100`. Access requires a
  constant-time `Bearer MCP_SERVICE_TOKEN` and `X-MCP-Audience: alloyiq-tools`. Only the
  five read-only tools exist. The gateway re-validates every envelope: schema, tool name,
  `tool_call_id` echo, and status/error consistency.
- **Cache:** optional Redis cache for public market data only, keyed by instrument,
  provider, interval and freshness bucket, so an entry is never served past its policy.
  A Redis failure counts as a cache miss.
- **Provider mode:** `PROVIDER_MODE=fixtures` serves clearly labelled SYNTHETIC data
  (sources `synthetic-*`, URLs at `fixtures.alloyiq.invalid`), so the app can be tried
  without keys. It is refused when `APP_ENV=prod`, and `catalog-verify --apply` refuses to
  enable instruments from fixture results.
- **Catalogue verification** (`alloyiq-admin catalog-verify --live --apply`) calls all four
  instrument tools for every supported horizon and enables an instrument only if every
  check passes, recording the evidence. It makes real provider calls, so it needs
  `--live` **and** `LIVE_PROVIDER_TESTS=1`. It has **not** been run: no keys were supplied,
  so all instruments remain disabled.

---

# Open decisions (owner input needed; do not invent answers)

From build prompt §21, plus anything found while building.

| ID | Decision | Blocks | Status |
|---|---|---|---|
| OD-1 | Yahoo Finance licensing before any non-personal use; choice of a licensed MCX/COMEX/LME vendor | Launch | open |
| OD-2 | Final real-spend caps (`REAL_SPEND_*`), and the demo grant, top-up, FX and markup assumptions | Launch; P4 defaults | open (defaults in `.env.example`) |
| OD-3 | Rate card check against Anthropic's live pricing page (cache-write prices, Haiku cache-read) | Activating rate card v1 (P4/P6) | open |
| OD-4 | Licence for retaining Tavily news text (MVP stores titles, URLs, dates, short excerpts only) | Storing full text | open |
| OD-5 | Hosting platform and region, managed Postgres, object storage, secret manager, production email | Production | open |
| OD-6 | Retention for provider-expense records (spec proposes 7 years; jurisdiction review) | Production | open |
| OD-7 | When to un-defer MFA, the admin UI, Google OIDC and subscriptions | Post-MVP | open |
| OD-8 | Incident-response ownership once anyone other than the owner uses the system | Any external user | open |
| OD-9 | Docker Desktop's VM has about 3 GB of RAM on the build machine. That is enough for P0; LangGraph, Next dev and Postgres together in P6/P7 may need 4-6 GB (Docker Desktop > Settings > Resources, or `.wslconfig`) | P6/P7 performance | open |

## ADR-014 Demo accounting, dummy payments and real-spend budgets (accepted, P4)

- **Ledger:** `billing.posting` builds every journal as pure data; the repository posts it
  with `ON CONFLICT (business_event_id) DO NOTHING`, so a repeated business event (opening
  grant, `topup:{checkout_id}`, `run_debit:{run_id}`, `refund:{refund_id}`) is a no-op. The
  member's wallet is `wallet:{account_id}` (positive = demo credit); counter-accounts are
  `sim_topup_source`, `sim_usage_income`, `sim_refunds` and `sim_adjustments`. None of them
  is revenue or cash. Corrections are reversal journals, never edits.
- **Balance:** `GET /account/balance` is computed from postings and reservations. The
  `wallets` row is a projection, updated in the same transaction and checked by a database
  `CHECK` (available >= 0); `alloyiq-admin reconcile payments [--repair]` compares and rebuilds
  it, and writes an incident on drift.
- **Lock order:** wallet -> account_day budget -> platform_day budget, everywhere
  (admission, settlement, refunds, pending-hold release).
- **Settlement rounding:** customer charges are summed at micro-INR precision and rounded
  once per run, half-up, to the paisa; the difference is stored as `rounding_adjustment` on
  the `run_debit` journal. A debit never exceeds the reservation (the platform absorbs any
  excess). With unknown usage the rest of the reservation stays as a pending hold for 24 h,
  then a worker scheduler releases it.
- **Unpriced categories:** a token category with tokens but no verified price makes the
  call's `cost_status=unknown` with a null total (never zero), and the member is not
  charged for that portion. Prompt caching is enabled only for models whose cache-read and
  cache-write prices are on the pinned rate card, so worst-case estimates need only input
  and output rates. Rate card v1 has no cache-write prices, so caching stays off until the
  owner verifies them (OD-3).
- **Rate-card activation:** `alloyiq-admin rate-card-activate <version>
  --confirm-prices-verified` marks the priced items verified, retires the active card and
  activates the draft. Without the flag it lists the prices to check and refuses.
- **Dummy payments:** the browser only picks an outcome for its own checkout
  (`/simulate`, CSRF and Idempotency-Key). The server writes a trusted `payment_events` row
  and NOTIFYs; the worker applies events one by one in the account's RLS context.
  Checkout states are final once paid, failed or expired, so duplicate and reordered events
  have no effect. A success received after `expires_at` expires the checkout instead. Expiry
  uses a deterministic event ID, so the scheduler is idempotent. A refund is checked at
  request time and again when applied; it is rejected if the credit was spent in between.
- **Top-up limits:** at most `DEMO_CHECKOUT_MAX_INR` per checkout and the plan's daily limit
  per UTC day (open plus paid checkouts count), whole paise only; 429 `PLAN_LIMIT_REACHED`
  with Retry-After.
- **Real-spend caps:** `BudgetService` keeps one row per scope and UTC day. The limit comes
  from configuration only and is re-applied on every lock. Exceeding the day caps at
  admission is 503 `BUDGET_EXCEEDED` with Retry-After until 00:00 UTC. A per-run cap overrun
  at estimate time is 422. With unknown usage, settlement counts the whole reservation as
  spent and leaves it `pending`.
- **Idempotency:** one helper (`application.idempotent.once`) claims the key, runs the
  mutation and saves the response in the same transaction. The fingerprint covers the
  concrete path and the body, so reusing a key on another checkout is a 409.

## ADR-015 Durable runs, worker leases and SSE (accepted, P5)

- **Admission** (`POST /research/runs`) is one transaction: idempotency claim, then a lock on
  the estimate row, the wallet lock (which also serialises the plan-limit count), the demo
  reservation, the real-spend reservation (account_day, then platform_day), the run, the
  question message, the job, an outbox row, `run.queued` (seq 1), `run.updated` and
  `balance.updated`. A failure anywhere rolls all of it back. The reservation is always the
  estimate maximum, even when the client authorises more (D059).
- **Claiming:** a `FOR UPDATE SKIP LOCKED` pick, oldest first, accounts with fewer active runs
  first, never beyond the plan's active-run limit. Each claim bumps `jobs.generation`, copies
  it to `research_runs.generation`, and records a `job_attempts` row. The lease is 30 s with
  a heartbeat every 10 s; a failed heartbeat cancels the work without side effects.
- **Fencing:** every workflow write goes through `RunHandle.tx()`, which locks the run row
  `WHERE generation = :mine`. Zero rows raises `LostAuthority`. Terminal changes are CAS
  updates over the legal source states (`domain.runs.sources_for`).
- **Cancel race:** `RunLifecycle.finish` tries the requested terminal state. If the run is
  `cancel_requested` by then, it resolves to `cancelled`, so a committed cancel always wins.
  A run that is already terminal makes cancellation return 200 with that snapshot. Cancelling a
  queued run is resolved in the API transaction with no model work.
- **Cancel latency:** the watcher listens on `run_cancel` and polls every 1 s. When it sees
  the flag, `RunHandle.check()` refuses new calls (`cancel_stop_calls_ms`) and the workflow
  task is cancelled, which cancels branch tasks and open streams. `cancel_stop_branches_ms` is
  recorded when the work has actually stopped.
- **Ambiguous calls:** an `llm_calls` intent with no completion, left by a crash, a cancel
  or a timeout, is marked `ambiguous` with `usage_status=unknown` and is non-billable. This
  happens when a run resumes and again at settlement. The member is not charged and the
  platform absorbs the cost (R043). Because the cost is unknown, settlement keeps the bounded
  pending hold and counts the whole real-spend reservation as spent.
- **Recovery sweeper** (worker, 15 s): an expired lease goes back to runnable with a
  `recovering` activity event. On the third attempt the job is dead, an incident is written
  and the run fails. Queued runs older than 5 minutes time out and their reservations are
  released.
- **SSE:** one LISTEN connection per process (`PgListener`). Notifications are only wake-ups;
  events are always read from `run_events` / `account_events`. Order of work: subscribe,
  check the cursor (400 if malformed, from another stream or ahead of it; 410 if older than
  retention), replay, then go live. Heartbeat every 15 s; session recheck every 10 s (a
  revoked session closes the stream); an inbox overflow past 100 notifications sends
  `stream.reset_required` and closes. The limit of 3 concurrent streams per member is
  enforced **in process**; a multi-replica deployment needs a shared counter (DV-06).
- **Workflow seam:** `RunExecutor.execute(handle) -> RunResult`. P5 ships
  `UnavailableWorkflow`, which fails runs explicitly with `workflow_not_installed`; P6
  plugs in the LangGraph workflow. P5 tests drive a fake graph that writes clearly synthetic
  usage through the real call-intent protocol.
