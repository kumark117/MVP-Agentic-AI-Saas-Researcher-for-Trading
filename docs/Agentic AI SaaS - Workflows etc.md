# AlloyIQ: Agentic AI SaaS

*Workflows, code map and quality attributes*

Kumar Krishnamoorthy and Claude & Team · 30 October 2026 · MVP P0–P9, commit 853c409+

> Generated from the same source as the matching .docx file in this folder.

AlloyIQ is an AI agent SaaS for metals-trading **research**. Three branch agents (Price, News, Technical) gather evidence in parallel through tools, and a Signal agent writes a cited report: BUY, SELL, HOLD or NO_SIGNAL. It never places orders. Payments are simulated (demo credit in INR, money collected is always zero); model and data costs are real, measured and capped.

This document walks through each workflow with the files, classes, functions, data structures and database operations involved, then covers the API, async model, MCP, evaluations, observability, security, reliability, and latency and cost. Paths are relative to `backend/src/alloyiq/` unless they start with `frontend/` or `backend/`.

## 1. Architecture at a glance

| Service (Docker Compose) | Technology | Role |
| --- | --- | --- |
| `caddy` | Caddy 2 | Single public entry at localhost:8080; routes `/api/*` to the API and everything else to the frontend; TLS and headers. |
| `frontend` | Next.js 16 App Router, TanStack Query, openapi-fetch | Member UI; strict nonce CSP set in `frontend/src/proxy.ts`. |
| `api` | FastAPI, Pydantic v2, SQLAlchemy 2 async + asyncpg | HTTP and SSE. Admits runs; never calls Claude itself. |
| `worker` | asyncio process (`worker/__main__.py`) | Claims jobs, runs the LangGraph research graph, plus periodic loops. |
| `mcp` | MCP Python SDK, Streamable HTTP | Private tool service: five read-only market and news tools. Not routed by Caddy. |
| `postgres` | PostgreSQL 16 | System of record: runs, ledger, events, evidence, checkpoints, audit. FORCE row-level security. |
| `redis` | Redis 7 | Rate-limit buckets and tool caches only (ADR-005); everything survives its loss. |
| `mailpit` | Mailpit | Captures verification and reset emails in development. |

### Code layers

| Layer | Folder | Contents |
| --- | --- | --- |
| Domain | `domain/` | Pure types and rules: `runs.py` (state machine), `money.py`, `llm.py` (LLM port), `payments.py`, `ports.py`, `errors.py`. |
| Application | `application/` | Use-case services: `ResearchService`, `WalletService`, `BudgetService`, `SettlementService`, `StreamService`, `AuthService`, `PurgeService`, … |
| Repositories | `repositories/` | SQL access: `Database`/`DbContext` (`db.py`), `RunRepository`, `WalletRepository`, `UsageRepository`, `EvidenceRepository`; table metadata in `schema.py`. |
| Adapters | `adapters/` | External systems: `AnthropicLLMGateway`, `FakeLLM`, Yahoo/Alpha Vantage/Tavily clients, `DummyPaymentProvider`, `PgListener`, `LocalArtifactStore`. |
| Workflows & agents | `workflows/`, `agents/` | LangGraph graph (`graph.py`), `RunExecutor` (`runner.py`), `ModelCaller`, `ToolCaller`, evidence rules; `BranchAgent`, `SignalAgent`, prompts. |
| Tools & MCP | `tools/`, `mcp_server/` | `ToolService` (provider logic), `MCPToolGateway` client, MCP server `build_mcp`/`build_app`. |
| API | `api/` | `app.py` (factory), `routes/*.py`, `schemas/*.py`, `middleware.py`, `deps.py`, `envelope.py`, `errors.py`. |
| Worker | `worker/` | `JobRunner` (`jobs.py`), loop registry `build_worker`/`WorkerParts` (`loops.py`). |
| Cross-cutting | `security/`, `observability/`, `billing/`, `stats/`, `indicators/`, `evaluations/`, `cli/` | Security helpers, logging and tracing, pricing and postings, statistics, technical indicators, eval harness, admin CLI. |

## 2. Main workflow: one research run

A run moves through nine steps. Steps 1–2 happen in the API request; steps 3–8 happen in the worker; step 9 runs throughout. Every state change is written to PostgreSQL first and only then announced, so a crash at any point is recoverable.

| Step | Where | Key code |
| --- | --- | --- |
| 1. Estimate | API | `ResearchService.estimate`, `billing/rates.py` `worst_case` |
| 2. Submit (admit) | API | `ResearchService.admit`, `once()`, `WalletService.reserve`, `BudgetService.reserve` |
| 3. Queue and claim | Worker | `JobRunner.claim_and_start`, `RunRepository.claim_job` |
| 4. Branch agents | Worker (graph) | `BranchAgent.run`, `ModelCaller.call`, `ToolCaller.call` |
| 5. Evidence checks | Worker (graph) | `merge_evidence`, `handoff_validation`, `evidence_minimum_gate`, `security/injection.py` |
| 6. Signal agent | Worker (graph) | `SignalAgent.write`, `SIGNAL_SCHEMA` |
| 7. Report checks | Worker (graph) | `validate_report`, `ungrounded_numbers`, `final_validation` |
| 8. Settle | Worker | `RunLifecycle.finish`, `SettlementService.settle` |
| 9. Live progress | API (SSE) + browser | `StreamService.stream`, `PgListener`, `StreamController.ts` |

### Pre-processing: what happens before a model sees any data

Claude never sees raw provider output or raw user input. Each item passes these steps in order:

| # | Step | Where | What it does |
| --- | --- | --- | --- |
| 1 | Validate the request | `api/schemas/research.py`, `ResearchService.estimate` | Pydantic checks the instrument, horizon, profile and question length; `fingerprint()` hashes the request so the estimate can't be swapped. |
| 2 | Freeze the run snapshot | `ResearchService.admit`, `workflows/versions.py` | Copies the instrument, horizon, approved models, limits and rate-card version into the run (`instrument_snapshot`); `run_versions` stores prompt and graph versions (`prompt_version` = sha256 of `agents/prompts/*.md`). |
| 3 | Resolve the symbol | `ToolService` | Maps the catalog instrument (enabled only) to the provider's symbol via `provider_mappings`; the model never chooses the instrument. |
| 4 | Fetch with limits | `ToolService`, `SafeHttpClient` | 8 s deadline, at most two retries for transient reads, egress policy; results cached in Redis (news for 10 min). |
| 5 | Validate provider data | `_validate_quote`, `_validate_bars` | Currency and unit must match the instrument, prices within plausible bounds, no future timestamps; bars sorted, duplicates dropped, high ≥ low. Bad data fails the tool: nothing is invented. |
| 6 | Compute indicators | `compute_indicators`, `indicators/core.py` | SMA, EMA, RSI, MACD, ATR and Bollinger bands from the validated bars, with a lookback set by `history_plan(horizon)`. |
| 7 | Build the news query | `ToolService.search_news` | Server-built query ("<metal> price market outlook" plus up to 80 chars of focus words), recency window by horizon, at most 8 articles. |
| 8 | Envelope and provenance | `ToolEnvelope`, `validate_envelope` | Wraps each result with source, as-of time and a provenance hash; malformed envelopes are rejected. |
| 9 | Normalise to evidence | `normalise` (`workflows/evidence.py`) | Turns envelopes into `EvidenceItem`s with server-assigned IDs, units and currencies. |
| 10 | Scan for injection | `security/injection.py` `scan` | Flags instruction-like text in news; quarantined items are replaced by a `redacted` record and logged. |
| 11 | Prepare the model view | `model_view`, `bundle_text` | External text is wrapped and marked as data, not instructions; the Signal agent gets only the validated bundle. |

### Step 1. Estimate

The member picks an instrument (e.g. `comex-gold-cont-ref`), a horizon (intraday, swing, position) and a model profile (deep, balanced, fast), and asks a question.

- **Route:** `POST /api/v1/research/estimates` → `create_estimate` (`api/routes/research.py`).

- **Logic:** `ResearchService.estimate` loads the instrument, the model profile and the **active rate card**, then calls `worst_case` (`billing/rates.py`) to bound tokens and tool calls. The result is a maximum simulated charge in INR and a maximum real cost in USD.

- **Data structures:** `RateCard` (prices per million tokens per model and category), `TokenUsage`, `WorstCase`; amounts use `Decimal` via `domain/money.py` (`micro`, `ceil_paise`, `per_mtok`).

- **DB:** `RunRepository.insert_estimate` writes a row to `estimates` with a fingerprint (`fingerprint()`) of the request and a short expiry.

### Step 2. Submit (admission)

- **Route:** `POST /api/v1/research/runs` → `start_run`. Requires the `Idempotency-Key` header and a CSRF token.

- **Idempotency:** `application/idempotent.py` `once()` stores the response in `idempotency_records`; a retried request with the same key returns the original result instead of starting a second run.

- **Logic:** `ResearchService.admit` checks plan limits (`RunRepository.plan_limits`, `count_open`), consumes the estimate (`consume_estimate`), and then, in one transaction:

  - reserves demo credit: `WalletService.reserve` → `WalletRepository.lock_wallet` (`SELECT … FOR UPDATE`) and `reserve` → rows in `reservations` and `reservation_events`;
  - reserves real spend: `BudgetService.reserve` → `BudgetRepository.lock` on `real_budget_accounts` for the run, the account-day and the platform-day, then `create_reservation` in `real_spend_reservations`;
  - creates the run and its job: `insert_run` (`research_runs`, status `queued`), `insert_job` (`jobs`), `create_conversation`/`insert_message`, and an `outbox` row.

- **Lock order:** Always wallet → account-day → platform-day, so concurrent admissions cannot deadlock.

- **Responses:** `202 Accepted` with the `run_id`; `409 INSUFFICIENT_CREDIT` for insufficient demo credit (`insufficient_credit`); `503 BUDGET_EXCEEDED` when a real-spend cap is full.

### Step 3. Queue, claim and lease

- **Code:** `worker/jobs.py` `JobRunner.run_forever` polls every 2 s and also wakes on Postgres `NOTIFY`. `claim_and_start` calls `RunRepository.claim_job`, which uses `SELECT … FOR UPDATE SKIP LOCKED` so two workers never take the same job.

- **Lease and fencing:** Each claim sets a lease and increments a **fencing generation** (`RunRepository.fence`). `heartbeat` renews the lease every 10 s. A worker whose generation is stale raises `LostAuthority` and stops writing.

- **State machine:** `domain/runs.py` `TRANSITIONS`: `queued` → `running` → `succeeded` | `incomplete` | `failed` | `timed_out`, with `cancel_requested` → `cancelled`. `RunRepository.transition` is a compare-and-swap (`UPDATE … WHERE status = :expected`), checked by `can_transition`.

- **Execution:** `RunExecutor.execute` (`workflows/runner.py`) builds a `RunContext` and `RunHandle` (deadline, cancel flag, `tx()`, `emit()`, `activity()`), then invokes `ResearchWorkflow.execute` (`workflows/graph.py`). Concurrency is 2 runs per worker by default.

### Step 4. Branch agents (Price, News, Technical)

`build_graph` wires the LangGraph `StateGraph`: `validate_request` → `policy_check` → the three branches **in parallel** → `merge_evidence`. Branch specs live in `agents/branch.py`:

| Agent | Node | Allowed tools |
| --- | --- | --- |
| Price | `price_branch` | `get_quote`, `get_fx_rate` |
| Technical | `technical_branch` | `get_technical_indicators`, `get_history` |
| News | `news_branch` | `search_news` |

- **Loop:** `BranchAgent.run` runs a bounded tool loop: send the prompt (`agents/prompts/*.md`) with strict tool definitions (`TOOL_DEFS`), execute any requested tool, feed results back, and stop at the model-call and tool-call limits. The final answer must match `BRANCH_FINDING_SCHEMA` and is re-validated by the Pydantic model `BranchFinding` (`agents/schemas.py`).

- **Model calls:** `ModelCaller.call` (`workflows/model_calls.py`) implements the **call-intent protocol**: `UsageRepository.intent` (row in `llm_calls`) → `authorize_call` against the real-spend reservation → `mark_in_flight` → `AnthropicLLMGateway.complete` → `complete` (writes `usage_records` with token categories and cost). If the outcome is unclear (timeout after sending), `mark_ambiguous` records **unknown usage**.

- **Tool calls:** `ToolCaller.call` (`workflows/tool_calls.py`) asks `PolicyEngine.authorise` whether this branch may use the tool, writes a `tool_calls` intent (`EvidenceRepository.tool_intent`), calls the MCP service, then `tool_finish` or `tool_ambiguous`. The server, not the model, fixes the instrument and builds the news query.

- **Checkpoints:** After each node LangGraph saves `GraphState` with `AsyncPostgresSaver` in schema `graph` (`workflows/factory.py` `PostgresCheckpointer`; tables from migration `0004_graph_checkpoints.py`). `RunHandle.node_started`/`node_finished` record progress in `run_nodes`, and `completed_output` lets a resumed run reuse finished work.

### Step 5. Evidence checks

- **Normalise:** `workflows/evidence.py` `normalise` turns tool envelopes into `EvidenceItem` records (id, source, kind, value, unit, currency, as-of time); `validate_envelope` rejects malformed tool output.

- **Injection quarantine:** `security/injection.py` `scan` checks news text for prompt-injection patterns; `InjectionScan.quarantined` items are excluded from the model view (`model_view`) and logged to `security_events`.

- **Handoff:** `handoff_checks` verifies units and currencies agree across branches (e.g. USD/oz vs INR conversions).

- **Gate:** `evidence_minimum` returns a `GateResult`. If evidence is insufficient the graph goes to `persist_incomplete_report` and the run ends `incomplete` with NO_SIGNAL. It never invents data.

- **DB:** `EvidenceRepository.insert_evidence` → `evidence_items`; `validation` → `validation_results`.

### Step 6. Signal agent

- **Code:** `agents/signal.py` `SignalAgent.write`. It has **no tools**: it only sees the validated evidence bundle (`bundle_text`).

- **Output:** JSON matching `SIGNAL_SCHEMA`, re-validated as `SignalOutput`: `signal`, `confidence_score`, `confidence_label`, `summary`, `supporting_evidence` and `opposing_evidence` (`Claim` with `evidence_ids`), `assumptions`, `risks`, `limitations`. Nullable fields use `anyOf` (the API rejects an enum inside a type union).

### Step 7. Report checks

- **Code:** `validate_report` returns a `ReportCheck`: every cited evidence ID must exist, every number in the text must be grounded in evidence (`ungrounded_numbers`), and no URLs are allowed in model text.

- **Repair:** If the check fails, `final_validation` allows **one** repair call, which is not billed to the member. If it still fails, the run is persisted as incomplete.

- **DB:** `assemble_report` builds the report; `EvidenceRepository.insert_report` writes `reports` and `report_citations`.

### Step 8. Settle

- **Code:** `RunLifecycle.finish` (`application/lifecycle.py`) sets the terminal state and calls `SettlementService.settle` (`application/settlement.py`).

- **Demo money:** Actual cost is converted to INR and rounded **once** to the paisa, half-up (`round_paise`). `billing/posting.py` `run_debit` and `split_settlement` build a balanced `Journal` of `Posting`s; `WalletService.settle` posts it to `ledger_transactions` and `ledger_postings`. The unused part of the reservation is released.

- **Real spend:** `BudgetService.settle` → `BudgetRepository.add_spend` and `close_reservation` on all three budget rows.

- **Unknown usage:** If any call is ambiguous, a bounded **pending hold** stays for 24 h; the `pending_hold_release` loop then releases it and the platform absorbs the cost.

- **Invariants:** A deferred trigger rejects any journal whose postings don't sum to zero; postings are immutable; journals are keyed by `business_event_id` so a retry cannot post twice.

### Step 9. Live progress (SSE)

- **Producer:** Every change calls `RunHandle.emit` → `append_run_event` (`repositories/events.py`) into `run_events` plus `pg_notify` in the same transaction.

- **API:** `GET /api/v1/research/runs/{run_id}/events` → `run_events`, and `GET /api/v1/events` → `account_events`. `StreamService.prepare` checks access and the 3-streams-per-member limit; `StreamService.stream` **subscribes (LISTEN) first, then replays** from Postgres after the `Last-Event-ID` cursor, so no event can fall in the gap.

- **Rules:** Bad cursor → `400`; cursor older than retention → `410`; heartbeat every 15 s; session re-checked every 10 s; buffer over 100 events → `stream.reset_required`.

- **Adapter:** `adapters/pg_listen.py` `PgListener` holds one asyncpg LISTEN connection and fans out to `Subscription` queues.

- **Browser:** `frontend/src/lib/sse/StreamController.ts` dedupes by event ID, reconnects with backoff, and probes the run snapshot after a reset. `RunDetail` (`frontend/src/features/runs/`) renders it; `Announcer.tsx` reads updates to screen readers.

### LangGraph node sequence

| # | Node | What it does |
| --- | --- | --- |
| 1 | `validate_request` | Re-checks the frozen run snapshot (instrument, horizon, versions). |
| 2 | `policy_check` | Confirms instrument enabled, models approved, budget present. |
| 3a–c | `price_branch`, `news_branch`, `technical_branch` | Parallel tool loops (fan-out). |
| 4 | `merge_evidence` | Joins the branches (fan-in) and normalises evidence. |
| 5 | `handoff_validation` | Unit and currency checks. |
| 6 | `evidence_minimum_gate` | Sufficient → signal; otherwise → incomplete report. |
| 7 | `signal_synthesis` | Signal agent writes the structured report. |
| 8 | `final_validation` | Citation and numeric grounding checks, one repair. |
| 9 | `persist_report` / `persist_incomplete_report` | Writes the report and ends the graph. |

`GraphState` (a `TypedDict`) carries `run_id`, `price`, `news`, `technical`, `evidence`, `handoff`, `gate`, `signal` and `outcome`. Any node that sets `outcome` stops the graph early (`stop_or`).

## 3. Supporting workflows

### 3.1 Register, verify email, log in

- **Routes:** `api/routes/auth.py` under `/api/v1/auth`: `csrf`, `register`, `verify_email`, `resend_verification`, `login`, `logout`, `logout_all`, `forgot_password`, `reset_password`, `reauthenticate`.

- **Logic:** `AuthService` (`application/auth.py`). Passwords use Argon2 (`security/passwords.py` `PasswordService`). Tokens are random and stored only as hashes (`security/tokens.py` `new_token`, `token_hash`). CSRF uses `CsrfTokens.issue`/`verify`.

- **Email:** Messages go to `email_outbox`; the worker's `email` loop (every 2 s, `application/email_dispatch.py`) sends them. In dev they land in Mailpit.

- **DB:** `users`, `auth_identities`, `accounts`, `account_memberships`, `sessions`, `auth_tokens`. Registration grants opening demo credit (`WalletService.grant_opening_credit`, `opening_grant` journal).

- **Sessions:** `/api/v1/me/sessions` lists them, `DELETE /me/sessions/{id}` revokes one; `AuthService.session_active` is re-checked by SSE streams.

### 3.2 Demo top-up (simulated payments)

- **Routes:** `POST /billing/credit-checkouts` → `create_checkout`; `POST /billing/checkouts/{id}/simulate` → `simulate_checkout`; `GET /billing/checkouts/{id}`; `GET /billing/invoices`; `POST /billing/refund-requests`. `POST /webhooks/payments/{provider}` is rejected (`reject_public_webhook`): no public webhook in the MVP.

- **Flow:** `BillingService.create_checkout` writes `checkouts` (`pending`). Simulating success makes `DummyPaymentProvider.checkout_event` write a **trusted** row to `payment_events`. The worker's `payment_events` loop (every 1 s, `BillingService.process_events`) applies it with a `topup` journal and issues a `receipts` row. Pending checkouts expire after 30 min (`expire_checkouts`).

- **Exactly once:** The checkout create uses `once()`; the journal is keyed by `business_event_id`. A double-clicked top-up therefore gives exactly one credit, which e2e journey 3 proves.

### 3.3 Cancel a run

- **Route:** `POST /research/runs/{run_id}/cancel` → `cancel_run` → `ResearchService.cancel`: CAS to `cancel_requested`.

- **Worker:** `RunHandle.observe_cancel` reacts to a LISTEN notification or a 1 s poll and raises `RunCancelled` at the next safe point; `RunLifecycle.finish` sets `cancelled` and settles only completed usage.

### 3.4 History, re-run, reports and export

- **History:** `HistoryService`: `/conversations`, `/conversations/{id}/messages`, and run feedback (`POST`/`PUT /research/runs/{id}/feedback`). Tables `conversations`, `messages`, `feedback`. Lists use keyset paging (`repositories/paging.py`).

- **Reports:** `ReportService.summary` and `models` (`/reports/summary`, `/reports/models`) compute exact p50/p95 with `stats/core.py` `percentile_cont`, plus `success_rate`.

- **Export:** `ExportService.create` (`POST /exports`) writes `export_jobs`; the `exports` loop (every 2 s, `process_pending`) builds a CSV with `security/csv_safe.py` `to_csv` (formula-injection neutraliser), stores it via `LocalArtifactStore` (`artifacts`), and `download` serves it via an **HMAC-signed link valid 15 minutes**.

### 3.5 Retention purge and account deletion

- **Request:** `POST /me/deletion-requests` → `AuthService.request_deletion`; it requires recent re-authentication and is scheduled 7 days ahead (`deletion_requests`).

- **Purge:** `PurgeService.run_once` (hourly `retention_purge` loop) applies retention to expired rows and runs `delete_account`: revoke sessions, cancel queued work, delete conversations, messages, feedback, reports, evidence, activity and exports, pseudonymise the user (`pseudonym`), and write `tombstones`. Demo ledger and provider-cost records are kept under their own retention. Migration `0005_privacy_purge.py` allows the system-scope deletes.

### 3.6 Evaluation harness

Covered in section 6.

### 3.7 Admin CLI

- **Entry:** `alloyiq-admin` (`cli/admin.py` with the `@command` registry), run as `alloyiq_migrator`.

- **Commands:** `summary`, `runs`, `audit`, `suspend` (`cli/admin_ops.py`); `grant-credit`, `reconcile payments [--repair]`, `rate-card-activate`, `catalog-verify [--live --apply --instrument]`, `eval [--live]` (`cli/admin_billing.py`, `application/catalog.py`). Output never includes emails, prompts or report text.

### 3.8 Worker periodic loops

| Loop | Interval | Function |
| --- | --- | --- |
| `jobs` (daemon) | continuous, 2 s poll + LISTEN | `JobRunner.run_forever` |
| `email` | 2 s | email outbox dispatch |
| `payment_events` | 1 s | `BillingService.process_events` |
| `checkout_expiry` | 30 s | `BillingService.expire_checkouts` |
| `pending_hold_release` | 5 min | `WalletService.release_due_pending` |
| `recovery_sweeper` | 15 s | `RecoverySweeper.sweep` |
| `exports` | 2 s | `ExportService.process_pending` |
| `retention_purge` | 1 h | `PurgeService.run_once` |

## 4. FastAPI route handlers

`api/app.py` builds the app and mounts every router under `/api/v1`. Each response is wrapped in `Envelope[T]` (`api/envelope.py`) with `data`, and errors map to stable codes (`api/errors.py`, `domain/errors.py`). Handlers are `async def` and depend on `api/deps.py` for the session, the account context and `CsrfProtected`. `api/contracts.py` keeps the OpenAPI contract that the frontend types are generated from.

| Method and path | Handler | Service call |
| --- | --- | --- |
| GET /auth/csrf | `csrf` | `CsrfTokens.issue` |
| POST /auth/register · /verify-email · /resend-verification | `register` · `verify_email` · `resend_verification` | `AuthService` |
| POST /auth/login · /logout · /logout-all · /reauthenticate | `login` · `logout` · `logout_all` · `reauthenticate` | `AuthService` |
| POST /auth/forgot-password · /reset-password | `forgot_password` · `reset_password` | `AuthService` |
| GET · PATCH /me; POST /me/password | `get_me` · `patch_me` · `change_password` | `AuthService` |
| GET /me/sessions; DELETE /me/sessions/{id} | `list_sessions` · `revoke_session` | `AuthService` |
| POST /me/deletion-requests | `request_deletion` | `AuthService` |
| GET /catalog/instruments · /models · /prices · /plans | `instruments` · `models` · `prices` · `plans` | catalog repository |
| GET /account/balance · /account/ledger | `balance` · `ledger` | `WalletService` |
| POST /billing/credit-checkouts; GET /billing/checkouts/{id} | `create_checkout` · `get_checkout` | `BillingService` |
| POST /billing/checkouts/{id}/simulate | `simulate_checkout` | `BillingService.simulate` |
| GET /billing/invoices; POST /billing/refund-requests | `invoices` · `refund_request` | `BillingService` |
| POST /webhooks/payments/{provider} | `payment_webhook` | `reject_public_webhook` |
| POST /research/estimates | `create_estimate` | `ResearchService.estimate` |
| POST /research/runs; GET /research/runs | `start_run` · `list_runs` | `ResearchService.admit` · `list_runs` |
| GET /research/runs/{id}; POST …/cancel | `get_run` · `cancel_run` | `snapshot` · `cancel` |
| GET /research/runs/{id}/report · /usage · /activity | `get_report` · `get_usage` · `get_activity` | `ResearchService` |
| GET /research/runs/{id}/events; GET /events | `run_events` · `account_events` | `StreamService` (SSE) |
| POST · GET /conversations; GET · POST …/messages | `create_conversation` · `list_conversations` · `list_messages` · `post_message` | `HistoryService` |
| POST · PUT /research/runs/{id}/feedback | `create_feedback` · `update_feedback` | `HistoryService.set_feedback` |
| GET /reports/summary · /reports/models | `summary` · `models_report` | `ReportService` |
| POST /exports; GET /exports/{id} · …/download | `create_export` · `get_export` · `download_export` | `ExportService` |

Health endpoints come from `api/health.py` (`build_health_router`) with a Postgres readiness probe (`adapters/postgres_probe.py`). `api/middleware.py` (`RequestContextMiddleware`, plain ASGI so SSE is never buffered) adds request IDs and access logs; rate limiting is applied per route through `api/deps.py`.

## 5. Async operations and MCP

### Async model

- **One event loop per process.** API and worker are fully `async`: SQLAlchemy 2 `AsyncEngine` with **asyncpg**, `httpx.AsyncClient` for providers, `AsyncAnthropic` for Claude, asyncpg LISTEN for notifications.
- **Transactions.** `Database.transaction(DbContext)` opens a transaction and sets `app.account_id`, `app.user_id` or `app.system` with `set_config(…, true)`, so row-level security applies to that transaction only.
- **Parallel branches.** LangGraph runs the three branch nodes concurrently; each branch's model and tool calls are awaited inside its own loop.
- **Worker concurrency.** `JobRunner` runs up to 2 jobs as asyncio tasks, each with its own heartbeat task and cancel watcher. Periodic loops run as separate tasks from `WorkerParts.loops`.
- **Blocking libraries kept off the loop.** `yfinance` is imported in a background thread at start-up (`YahooClient` loader, `ToolService.warm`), and calls are serialised by an `asyncio.Lock` with a 3 s per-attempt timeout inside the tool deadline.
- **Deadlines.** `RunHandle.remaining_s` bounds every call; `DeadlineExceeded` ends the run `timed_out`.
- **Windows note.** Psycopg's async mode needs a selector event loop; the CLI `main` and the test `conftest` set it.

### MCP tool service

- **Server.** `mcp_server/app.py` `build_mcp` registers five read-only tools: `get_quote`, `get_history`, `get_technical_indicators`, `get_fx_rate`, `search_news`. `build_app` serves them over **Streamable HTTP** at `/mcp` (Starlette), with transport security settings.
- **Access.** `ServiceTokenGuard` requires `Authorization: Bearer <MCP_SERVICE_TOKEN>` (compared with `hmac.compare_digest`) and `X-MCP-Audience: alloyiq-tools`. Caddy never routes to it; only the worker can reach it. User cookies or tokens are never passed through.
- **Client.** `tools/gateway.py` `MCPToolGateway.session` opens a `streamable_http_client` session; every result is checked with `validate_envelope` against `ToolEnvelope` (`tools/schemas.py`). `InProcessToolGateway` is the same interface without the network, used by tests, the eval harness and as a hosted fallback (`TOOL_GATEWAY=in_process`). On Render the MCP server runs inside the worker's container on localhost (DV-15).
- **Providers.** `ToolService` (`tools/service.py`) calls Yahoo Finance (quotes and history), Alpha Vantage (FX) and Tavily (news) through `SafeHttpClient`; `compute_indicators` uses `indicators/core.py` (`sma`, `ema`, `rsi`, `macd`, `atr`, `bollinger`). Results are cached in Redis (`tools/cache.py` `RedisCache`).
- **Model-facing tool definitions** are byte-identical across calls (`TOOL_DEFS`, `strict: true`) so prompt caching works; the model chooses only which tool, never the instrument.

## 6. Data structures and database

### Roles and isolation

- Three roles: `alloyiq_api` (member requests), `alloyiq_worker` (jobs and loops), `alloyiq_migrator` (migrations and admin CLI).
- **FORCE row-level security** on member tables: policies compare `account_id` to `current_setting('app.account_id')`; system work sets `app.system`.
- Migrations (Alembic, `backend/migrations/versions/`): `0001_initial_schema`, `0002_rls_grants_triggers`, `0003_auth_lookups`, `0004_graph_checkpoints`, `0005_privacy_purge`, `0006_artifact_content` (artifact bytes for hosted deploys, DV-15). Each has a working downgrade, tested down-and-up.

### Tables by area

| Area | Tables |
| --- | --- |
| Identity | `users`, `auth_identities`, `accounts`, `account_memberships`, `sessions`, `auth_tokens`, `email_outbox` |
| Catalog and pricing | `instruments`, `provider_mappings`, `model_profiles`, `rate_cards`, `rate_card_items`, `plans`, `provider_health` |
| Demo money | `wallets`, `reservations`, `reservation_events`, `ledger_transactions`, `ledger_postings`, `checkouts`, `refunds`, `payment_events`, `receipts` |
| Real spend | `real_budget_accounts`, `real_spend_reservations`, `expense_reconciliation_items` |
| Runs | `estimates`, `idempotency_records`, `research_runs`, `run_nodes`, `jobs`, `job_attempts`, `outbox`, `incidents` |
| Model and tools | `llm_calls`, `usage_records`, `tool_calls`, `validation_results`, `evidence_items`, `reports`, `report_citations` |
| Events | `run_events`, `account_events`, `account_event_cursors` |
| History and export | `conversations`, `messages`, `feedback`, `export_jobs`, `artifacts` |
| Privacy | `deletion_requests`, `tombstones` |
| Observability and audit | `audit_events`, `security_events`, `trace_outbox`, `evaluation_runs`, `evaluation_scores` |
| Graph (schema `graph`) | LangGraph `AsyncPostgresSaver` checkpoint tables |

Every table carries a `retention=…` comment (`repositories/schema.py`), which the purge job and the docs rely on.

### Key Python data structures

| Structure | File | Purpose |
| --- | --- | --- |
| `RunStatus`, `BillingStatus`, `TRANSITIONS` | `domain/runs.py` | Run state machine as Literal types and an allowed-moves map |
| `LLMRequest`, `LLMResponse`, `ToolUse`, `LLMError` | `domain/llm.py` | Provider-neutral model call port |
| `RunContext`, `RunHandle`, `RunResult` | `workflows/runner.py` | Per-run execution context, authority and deadline |
| `GraphState` | `workflows/graph.py` | LangGraph state (TypedDict) |
| `EvidenceItem`, `GateResult`, `ValidationOutcome` | `workflows/evidence.py` | Normalised evidence and gate decisions |
| `BranchFinding`, `SignalOutput`, `Claim` | `agents/schemas.py` | Strict structured outputs (Pydantic, frozen, extra=forbid) |
| `Posting`, `Journal`, `SettlementSplit` | `billing/posting.py` | Double-entry demo ledger |
| `RateCard`, `TokenUsage`, `CallCost`, `WorstCase` | `billing/rates.py` | Pricing and worst-case bounds |
| `WalletBalance`, `ReservationRecord` | `repositories/ledger.py` | Wallet projection and holds |
| `BudgetRow`, `SpendReservation` | `repositories/budget.py` | Real-spend caps and headroom |
| `CallRecord` | `repositories/usage.py` | One model call's intent and outcome |
| `ToolEnvelope`, `ToolError` | `tools/schemas.py` | Validated tool results |
| `Span`, `TraceSink` | `observability/tracing.py` | Redacted tracing |

## 7. Evaluations

- **Harness:** `evaluations/harness.py` `EvalHarness.run` runs each `Case` through the **real research graph** and scores deterministic checks: schema, citation support, numeric grounding, signal semantics, blocked actions (quarantine, unapproved tools), expected outcome and abstention policy.
- **Dataset:** `evaluations/datasets/v1/cases.yaml`, 37 cases loaded by `load_dataset` into `Dataset`/`Case`/`Expect`. They include adversarial cases (prompt injection in news, attempts to use other tools, missing data).
- **Offline (default):** providers are synthetic fixtures and Claude is a scenario-driven fake (`adapters/fake_llm.py`), so it tests the pipeline's guarantees for free.
- **Live:** `alloyiq-admin eval --live` (owner approval, `LIVE_PROVIDER_TESTS=1`) runs the `live_ok` cases with real providers and Claude; an optional sampled judge (`claude-opus-5-5`) adds a relevance score, counted as platform cost.
- **Release gates:** 100% on schema, numeric grounding and adversarial checks; at least 95% citation support. `EvalSummary.gates_passed` must be true.
- **Storage:** results and versions go to `evaluation_runs` and `evaluation_scores` (`CaseResult`).
- **Latest live result:** 10/10 cases passed on every check.
- **Other tests:** 240 backend tests (pytest), 10 frontend unit tests, and 9 Playwright e2e runs (7 journeys; the 2 accessibility journeys also run on mobile) on a separate synthetic stack (`PROVIDER_MODE=fixtures`, `LLM_MODE=fake`, both refused in production).

## 8. Observability and database audit

- **Audit trail:** `repositories/audit.py` `audit()` writes `audit_events` (actor kind, action, outcome, target, run, redacted metadata) for auth, billing, ledger, admin and privacy actions, in the same transaction as the change. `alloyiq-admin audit --account --action` reads it.
- **Security events:** `security_event()` writes `security_events`, e.g. `injection.quarantined`, failed logins and rate-limit hits.
- **Run activity:** `run_events` and `run_nodes` record every state change and node with timings; `GET /research/runs/{id}/activity` exposes them to the member.
- **Usage records:** `llm_calls` and `usage_records` hold per-call model, token categories, cost and status (`known` / `unknown`); `tool_calls` holds per-tool outcomes.
- **Incidents:** `incidents` records dead jobs, projection drift and other conditions needing owner attention; `alloyiq-admin summary` shows open ones.
- **Tracing:** `observability/tracing.py` `TraceSink` creates `Span`s for graph nodes and calls; `redact()` strips secrets and personal data. `LangSmithTraceSink` sends them to LangSmith via the durable `trace_outbox`; if delivery fails, the run is flagged `observability_gap` rather than blocked. `LocalTraceSink` is used offline.
- **Logs:** structured JSON logs (`observability/logging.py`) with request and run IDs; secrets are never logged.
- **Ledger reconciliation:** `alloyiq-admin reconcile payments` compares the wallet projection with postings (`WalletService.verify_projection`); `--repair` rebuilds it and writes an incident.

## 9. Security

- **Tenant isolation:** FORCE RLS on every member table, per-transaction context, three least-privilege DB roles.
- **Authentication:** Argon2 password hashing (`PasswordService`), hashed one-time tokens, server-side sessions with revocation, re-authentication for sensitive actions (`reauthenticate`).
- **CSRF and CORS:** `CsrfTokens` on all mutations (`CsrfProtected` dependency) plus an `Origin` check.
- **Rate limiting:** `security/ratelimit.py` `RedisRateLimiter`, falling back to a stricter `InMemoryRateLimiter` when Redis is down.
- **Frontend:** strict nonce-based Content-Security-Policy in `frontend/src/proxy.ts`; typed API client from the OpenAPI contract.
- **Prompt injection:** news content is scanned and quarantined (`security/injection.py`); the Signal agent has no tools; branch tools are allow-listed per agent (`PolicyEngine`); the server fixes the instrument and query.
- **Output safety:** citations must exist, numbers must be grounded, no URLs in model text; CSV exports neutralise formula injection (`csv_safe.neutralise`).
- **Egress control:** `security/egress.py` `EgressPolicy` and `SafeHttpClient` block private and loopback addresses (SSRF).
- **MCP:** private network only, bearer token plus audience header, constant-time comparison.
- **Secrets:** API keys only in `.env` (never `.env.example`); `config.secret_or_none` treats placeholders and comments as unset; tracing redacts secrets; `gitleaks`, `pip-audit` and `pnpm audit` are clean.
- **Signed links:** exports use HMAC-signed links that expire after 15 minutes; rotating `SESSION_SECRET` invalidates sessions, CSRF tokens and links.
- **Privacy:** 7-day scheduled deletion, pseudonymisation, tombstones, table-level retention.

## 10. Reliability

- **Durable jobs:** `jobs` with `SKIP LOCKED` claiming, leases renewed every 10 s, and fencing generations so a stale worker cannot write (`LostAuthority`).
- **Crash recovery:** `RecoverySweeper.sweep` (every 15 s) requeues expired leases; after 3 attempts (`MAX_ATTEMPTS`) the run fails with an incident; queued runs expire after 5 minutes (`QUEUE_EXPIRY_S`).
- **Resume without double charging:** LangGraph checkpoints plus `completed_output` and `UsageRepository.completed_result` reuse finished nodes and model calls.
- **Guarded state changes:** compare-and-swap transitions validated by `can_transition`.
- **Exactly-once money:** `Idempotency-Key` with `once()`, journals keyed by `business_event_id`, a deferred balance trigger and immutable postings.
- **Ambiguous calls:** the call-intent protocol records unknown usage instead of guessing; a 24 h bounded hold, then the platform absorbs it.
- **Model fallbacks:** the gateway uses `max_retries=0`; `ModelCaller` retries only on refusal or overload and only to approved fallback models (deep profile: Opus 5.5 → Sonnet 5.5). Server-side fallbacks are deliberately unused.
- **Never fabricate:** missing data ends the run `incomplete` with NO_SIGNAL and releases the unused reservation.
- **Degraded dependencies:** Redis loss affects only caches and rate limits; SSE replays from Postgres; `PgListener` reconnects, and polling covers the gap.
- **Honest limits:** single host, manual backups, no claimed SLO (DV-06). Runbook: `docs/ops/runbook.md`.

## 11. Latency and cost (LLM)

### Model profiles

| Profile | Primary model | Fallback | Branch effort | Signal effort |
| --- | --- | --- | --- | --- |
| deep (default) | `claude-opus-5-5` | `claude-sonnet-5-5` | low | high |
| balanced | `claude-sonnet-5-5` | none | low | medium |
| fast | `claude-haiku-5-5` | none | low | medium |

Calls use the official `anthropic` SDK (`AsyncAnthropic`) in `adapters/anthropic_gateway.py`, with adaptive thinking, `output_config` effort and `json_schema` output format, and strict tools with `tool_choice: auto`.

### Latency

- Branch agents run **in parallel**, so wall-clock time is roughly the slowest branch plus the Signal call, not the sum.
- **Low effort** for branch tool loops and **higher effort** only for the Signal synthesis spends thinking where it matters.
- **Prompt caching:** stable system prompts and byte-identical tool definitions let repeated calls hit the cache, lowering cost and time to first token.
- Tool results are cached in Redis; `yfinance` is preloaded off the call path (its import alone takes about 8 s).
- Each run records `end_to_end_ms` and `time_to_first_model_token_ms`; `/reports/models` gives exact p50 and p95 per model with `percentile_cont`.

### Cost control

- **Worst-case pricing before admission:** `worst_case` bounds tokens and tool calls, and the reservation covers that maximum.
- **Hard real-spend caps (USD):** per run `1.50`, per account per day `5.00`, per platform per day `20.00` by default (`REAL_SPEND_*` in `.env`). Demo credit can never raise them. Caps reset at 00:00 UTC.
- **Per-call authorisation:** `BudgetService.authorize_call` checks remaining headroom before **every** model call.
- **Exact accounting:** `price_usage` prices each token category from the active rate card (v1, owner-verified; e.g. Opus 5.5 at USD 4 input and 20 output per million tokens) into `CallCost` lines. Costs are calculated, not yet reconciled with Anthropic invoices (DV-07).
- **Repair is not billed** to the member, and unknown usage is absorbed by the platform.
- **Observed:** a live gold swing run on the deep profile cost about USD 0.21 and returned SELL with 15 citations; total live verification spend, including `eval --live`, was about USD 2.42 with no unknown usage.

## 12. Appendix

### Frontend map

| Path (under `frontend/src/`) | Purpose |
| --- | --- |
| `lib/api/client.ts`, `queries.ts` | `openapi-fetch` client typed from the API contract; TanStack Query hooks |
| `lib/api/money.ts` | Decimal-safe money formatting |
| `lib/sse/StreamController.ts` | SSE dedupe, backoff, reset probe (unit tested) |
| `proxy.ts` | Nonce CSP and request proxying |
| `components/AppShell.tsx`, `ui.tsx`, `Announcer.tsx` | Layout, shared UI, polite live region |
| `features/auth`, `research`, `runs`, `account`, `security`, `history`, `reports` | Feature screens: `ResearchForm`, `ResearchEntry`, `RunDetail`, `Billing`, sessions, history, reports |
| `frontend/e2e/journeys.spec.ts`, `a11y.spec.ts` | Playwright journeys and axe accessibility checks |

Decisions and deviations: `docs/decisions.md` (ADRs), `docs/deviations.md` (DV-xx), requirement mapping in `docs/traceability.csv`.

### One-time setup before the first research run

| # | Step | Command or file | Notes |
| --- | --- | --- | --- |
| 1 | Install prerequisites | Docker Desktop (WSL 2), Git, Node.js 24 + pnpm, uv | uv manages Python 3.12. |
| 2 | Allow local scripts | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` | Once per Windows user. |
| 3 | Start the stack | `scripts\dev.ps1` | First run creates `.env` with random secrets (`SESSION_SECRET`, `MCP_SERVICE_TOKEN`, DB passwords), builds and starts all services, and waits until healthy. |
| 4 | Add provider keys | `.env` only (never `.env.example`) | `ANTHROPIC_API_KEY`, `ALPHAVANTAGE_API_KEY`, `TAVILY_API_KEY`; optional `LANGSMITH_API_KEY`. Comments go on their own lines. Restart with `docker compose up -d api worker mcp`. |
| 5 | Migrate and seed | `scripts\seed.ps1` | Runs migrations as `alloyiq_migrator`, then seeds the Demo plan, model profiles, draft rate card v1 and the instrument catalogue. Safe to re-run. |
| 6 | Verify the catalogue | `alloyiq-admin catalog-verify --live --apply` | Needs `LIVE_PROVIDER_TESTS=1`. Checks each instrument against real providers; failing ones are disabled with a reason. `--instrument` re-checks one. |
| 7 | Activate the rate card | `alloyiq-admin rate-card-activate v1-2026-10-06 --confirm-prices-verified` | Only after checking each price against Anthropic's pricing page; active cards are immutable. |
| 8 | Check spend caps | `REAL_SPEND_*` in `.env` | Defaults: USD 1.50 per run, 5.00 per account-day, 20.00 per platform-day. |
| 9 | Smoke test | `alloyiq-admin eval`, then one run at http://localhost:8080 | Offline eval must report `release_gates_passed: true`; `eval --live` costs real money. |

Admin commands run inside the API container: `docker compose exec api alloyiq-admin <command>`. Daily checks and incident procedures are in `docs/ops/runbook.md`.
