# IntrAI agentic SaaS: AlloyIQ

*Processes, async tasks, memory, history, data and auditing*

Kumar Krishnamoorthy and Claude & Team · 30 October 2026

> Generated from the same source as the matching .docx file in this folder.

A companion to **Agentic AI SaaS - Workflows etc.docx**, which walks through each workflow step by step. This document describes what runs, where state lives, and how the system records what happened. Paths are relative to `backend/src/alloyiq/` unless they start with `frontend/`, `docs/` or a repo-root file name.

## 1. Project information

| Item | Value |
| --- | --- |
| Product | AlloyIQ: an AI agent SaaS for metals-trading **research** (BUY / SELL / HOLD / NO_SIGNAL with citations). It never places orders. |
| Repository | github.com/kumark117/MVP-Agentic-AI-Saas-Researcher-for-Trading (public) |
| Authors | Kumar Krishnamoorthy (product owner: specifications, decisions, live verification); Claude & Team (Claude Opus 5.5 by Anthropic, with Claude Code: design and implementation) |
| Status | MVP phases P0–P9 complete and live-verified; hosted deployment (Vercel + Render) prepared |
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 async + asyncpg, LangGraph, official `anthropic` SDK, MCP Python SDK |
| Frontend | Next.js 16 (App Router, Cache Components), TanStack Query, openapi-fetch, Tailwind CSS 4 |
| Data stores | PostgreSQL 16 (system of record, row-level security), Redis 7 (caches and rate limits only) |
| Models | Claude Opus 5.5 (deep, default), Sonnet 5.5 (balanced), Haiku 5.5 (fast) |
| Data providers | Yahoo Finance (reference quotes and history), Alpha Vantage (FX and reference series), Tavily (news) |
| Money | Demo credit in INR is simulated (money collected is always zero); model and data costs are real, in USD, and capped |
| Quality evidence | Backend pytest suite, 10 frontend unit tests, 9 Playwright e2e runs, a 37-case evaluation harness; live `eval --live` passed 10/10 |

## 2. Processes and services

### Local stack (Docker Compose, `docker-compose.yml`)

| Service | Process (command) | Port | Role |
| --- | --- | --- | --- |
| `caddy` | Caddy 2 (`Caddyfile`) | 8080 (public) | Single entry: `/api/*` → api, everything else → web; streams SSE unbuffered |
| `web` | `next dev` (Node.js 24) | 3000 (internal) | Member UI; nonce CSP in `frontend/src/proxy.ts` |
| `api` | `uvicorn alloyiq.api.app:create_app --factory` | 8000 (internal) | HTTP + SSE; admits runs; never calls Claude |
| `worker` | `python -m alloyiq.worker` | none | Research runs and 7 periodic loops |
| `mcp` | `python -m alloyiq.mcp_server` (uvicorn, Streamable HTTP) | 8100 (internal) | Five read-only market and news tools |
| `postgres` | PostgreSQL 16 | 5432 (localhost only) | System of record; LangGraph checkpoints in schema `graph` |
| `redis` | Redis 7, no persistence | 6379 (localhost only) | Rate-limit buckets and tool caches |
| `mailpit` | Mailpit | 8025 (UI) | Captures outgoing email in development |

### Hosted (Vercel + Render, `render.yaml`)

| Where | Service | Plan | Notes |
| --- | --- | --- | --- |
| Vercel | Next.js frontend | Hobby (free) | Proxies `/api/*` to Render (`API_ORIGIN` in `frontend/next.config.ts`), so cookies stay first-party |
| Render | `alloyiq-api` (web) | Starter (paid) | Always on (a free instance sleeps after 15 min idle); started through `python -m alloyiq.cli.render exec --` |
| Render | `alloyiq-worker` (background worker) | Starter (paid) | Also runs the MCP tool server on `127.0.0.1:8100` in the same container (`alloyiq.cli.render worker`); pre-deploy creates roles, migrates and seeds |
| Render | `alloyiq-db` (Postgres 16) | Free | 1 GB; expires after 30 days unless upgraded; also stores artifact bytes (`ARTIFACT_BACKEND=postgres`) |
| Render | `alloyiq-cache` (Key Value) | Free | Replaces Redis |
| Gmail | SMTP `smtp.gmail.com:587` | Free | STARTTLS plus an app password; replaces Mailpit |

The hosted differences are recorded as deviation DV-15 in `docs/deviations.md`; the steps are in `docs/ops/deploy-render-vercel.md`.

## 3. FastAPI request handling and asyncio tasks

### API process (FastAPI on uvicorn)

- **One event loop.** Every route handler is an `async def` coroutine; database access uses SQLAlchemy's `AsyncEngine` over asyncpg, so a slow query never blocks other requests.
- **App factory:** `api/app.py` `create_app` builds the services (`api/deps.py` `build_services`) and mounts all routers under `/api/v1`. Its `lifespan` closes the LISTEN connection and the DB pool on shutdown.
- **Per-request flow:** `RequestContextMiddleware` (plain ASGI, so SSE is never buffered) assigns `request_id` → dependencies resolve the session, account context and CSRF check → the handler calls an application service → `Database.transaction(DbContext)` sets the RLS context with `set_config` → the response is wrapped in `Envelope[T]`.
- **SSE streams:** each open stream is one long-lived async generator in `StreamService.stream`: a heartbeat every 15 s, a session re-check every 10 s, a 2 s poll fallback, and at most 100 buffered events before `stream.reset_required`.
- **Shared LISTEN connection:** `LazyListener` starts one `PgListener` per API process on first use; it runs as the background task `pg-listener` and fans notifications out to per-stream `Subscription` queues.
- **Rate limiting:** `RedisRateLimiter` (async Redis calls), falling back to `InMemoryRateLimiter` when Redis is unavailable.

### Worker process (`worker/__main__.py`)

| asyncio task | Created by | Interval / lifetime | Does |
| --- | --- | --- | --- |
| `email` | `_periodic` | every 2 s | Sends `email_outbox` rows (SMTP via `asyncio.to_thread`) |
| `payment_events` | `_periodic` | every 1 s | Applies trusted payment events to the ledger |
| `checkout_expiry` | `_periodic` | every 30 s | Expires pending checkouts after 30 min |
| `pending_hold_release` | `_periodic` | every 5 min | Releases 24 h unknown-usage holds |
| `recovery_sweeper` | `_periodic` | every 15 s | Requeues expired leases; fails jobs after 3 attempts |
| `exports` | `_periodic` | every 2 s | Builds CSV exports |
| `retention_purge` | `_periodic` | every 1 h | Retention expiry and account deletion |
| `jobs` (daemon) | `_daemon` | whole process | `JobRunner.run_forever`: claims jobs (2 s poll + LISTEN) |
| `pg-listener` | `PgListener.start` | whole process | LISTEN for job and cancel notifications |
| `run-<run_id>` | `JobRunner.claim_and_start` | one per claimed run (max 2) | `JobRunner.process` |
| execution task | `JobRunner.process` | one per run | `RunExecutor.execute`: the LangGraph graph |
| heartbeat task | `JobRunner.process` | every 10 s during a run | Renews the lease; stops the run if authority is lost |
| cancel watcher | `JobRunner.process` | 1 s poll + LISTEN | Stops the run at a safe point after a cancel |

- **Shutdown:** SIGTERM/SIGINT set a shared `asyncio.Event`; every loop waits on it with `asyncio.wait_for`, so they stop promptly and the tasks are gathered.
- **Inside a run:** LangGraph runs the Price, News and Technical branch nodes concurrently; each branch awaits its own Claude and tool calls.
- **Threads, not tasks:** blocking libraries run off the loop: `yfinance` is imported in a background thread at start-up and called under an `asyncio.Lock`; SMTP and local artifact writes use `asyncio.to_thread`.

### MCP process

`mcp_server/__main__.py` runs uvicorn with a Starlette app from `build_app`: the MCP Streamable HTTP session manager at `/mcp`, guarded by `ServiceTokenGuard`. Each tool call is a coroutine that calls `ToolService`. On Render it runs inside the worker's container on `127.0.0.1:8100`: `alloyiq.cli.render worker` (`supervise`) starts it, waits for `/health`, then starts the worker, and stops both if either exits. `TOOL_GATEWAY=in_process` (`InProcessToolGateway`) remains as a fallback that calls `ToolService` directly.

## 4. Short-term memory (STM)

Short-term memory is state that exists only for the duration of one run, one request or one cache window. Its main purpose is to let a run finish correctly, or resume after a crash, without carrying anything into the next run.

| STM | Where | Lifetime | Contents |
| --- | --- | --- | --- |
| Branch agent transcript | `BranchAgent.run` (`agents/branch.py`): `messages` list | One branch in one run | Server-built context (instrument, horizon, the member's question wrapped as untrusted data) → assistant `tool_use` turns → `tool_result` turns. Bounded by model-call and tool-call limits |
| Signal agent transcript | `SignalAgent.write` (`agents/signal.py`) | One run | The validated evidence bundle (`bundle_text`), the structured answer, and at most one repair turn |
| Graph state | `GraphState` (`workflows/graph.py`) | One run | `run_id`, branch results (`price`, `news`, `technical`), `evidence`, `handoff`, `gate`, `signal`, `outcome` |
| Durable checkpoints | LangGraph `AsyncPostgresSaver`, schema `graph`, `thread_id = run_id` | Until the run ends (and retention) | `GraphState` after each node, so a recovered run resumes instead of restarting |
| Run handle | `RunHandle` / `RunContext` (`workflows/runner.py`) | One run, in memory | Deadline, cancel flag, fencing generation, frozen run snapshot |
| Completed-call reuse | `run_nodes`, `llm_calls`, `usage_records` | One run (rows kept by retention) | Finished node outputs and model results, so a resumed run never pays twice |
| Tool cache | Redis (`tools/cache.py` `RedisCache`) | Minutes (news: 10 min) | Validated provider results keyed by request hash |
| Prompt cache | Anthropic, provider-side | Minutes | Cached prefixes of stable system prompts and byte-identical tool definitions |
| Stream buffers | `Subscription` queues (`adapters/pg_listen.py`) | One SSE connection | Up to 100 pending events |
| Browser state | TanStack Query cache; `StreamController` seen-event IDs | One browser tab | Fetched data and de-duplicated live events |

## 5. Long-term memory (LTM)

Long-term memory is what the system keeps after a run ends, all in PostgreSQL, each table with a retention class.

- **Research memory:** `research_runs` (request, frozen snapshot, versions, timings, billing state), `reports` and `report_citations`, `evidence_items` (validated evidence), `validation_results`.
- **Conversation memory:** `conversations` and immutable `messages` (≤ 4000 characters each); a follow-up run records `message_cutoff_id`, the point in the conversation it answers.
- **Member feedback:** `feedback` (one rating per user and run).
- **Cost and money memory:** `llm_calls`, `usage_records`, `tool_calls`, and the immutable demo ledger (`ledger_transactions`, `ledger_postings`).
- **Evaluation memory:** `evaluation_runs` and `evaluation_scores` keep each harness run with its versions.

### Design choice: LTM is not fed back into the agents

- Every run starts with **fresh evidence only**. The agents receive the instrument, horizon and current question; earlier reports, messages and evidence are **not** added to their prompts.
- **Why:** every claim must cite evidence fetched for this run, so stale prices or old news can't leak into a new signal. It also means text injected into an earlier conversation can't steer later runs.
- There is no vector store, embedding index or semantic recall in the MVP. If cross-run memory were added, it would have to pass the same evidence validation and injection quarantine as tool data.

## 6. History

| History | Stored in | Shown through |
| --- | --- | --- |
| Runs and their status | `research_runs` | `GET /research/runs`, `GET /research/runs/{id}` |
| Run activity timeline | `run_events`, `run_nodes` | `GET /research/runs/{id}/activity`; the run page |
| Reports | `reports`, `report_citations`, `evidence_items` | `GET /research/runs/{id}/report` |
| Usage and cost per run | `llm_calls`, `usage_records` | `GET /research/runs/{id}/usage` |
| Conversations | `conversations`, `messages` | `/conversations`, `/conversations/{id}/messages` |
| Re-run | always a new estimate, never a silent repeat | History page → Re-run (e2e journey 5) |
| Aggregate reports | computed from runs and usage | `/reports/summary`, `/reports/models` (exact p50 / p95) |
| Exports | `export_jobs`, `artifacts` | `POST /exports`, signed 15-minute download link |
| Wallet history | `ledger_transactions`, `ledger_postings` | `GET /account/ledger` |
| Live-event replay | `run_events`, `account_events` (30 days) | SSE with `Last-Event-ID`; `410` once older than retention |

### Retention classes

| Class | Kept for | Examples |
| --- | --- | --- |
| `ephemeral_7d` | 7 days | `jobs`, `idempotency_records`, export artifacts |
| `sse_30d` | 30 days | `run_events`, `account_events` |
| `activity_90d` | 90 days | `evidence_items`, `tool_calls`, `artifacts`, `trace_outbox` |
| `product_12m` | 12 months | `research_runs`, `reports`, `conversations`, `messages`, `feedback`, ledger |
| `security_12m` | 12 months | `audit_events`, `security_events`, `tombstones` |
| `financial_policy` | Financial retention policy | `llm_calls`, `usage_records` |
| `identity`, `auth_short`, `platform` | Account lifetime / short / operator | `users`, `sessions`, `incidents` |

`PurgeService.run_once` applies these hourly. Each table's class is written into its database comment (`repositories/schema.py`).

## 7. Data and configuration files

| File(s) | Purpose |
| --- | --- |
| `agents/prompts/_common.md`, `price.md`, `news.md`, `technical.md`, `signal.md` | Versioned system prompts; `prompt_version` is a sha256 over them, stored on every run |
| `evaluations/datasets/v1/cases.yaml` | The 37 evaluation cases (normal, missing data, adversarial) |
| `docs/contracts/openapi.json` | API contract; the frontend's `frontend/src/lib/api/schema.d.ts` is generated from it |
| `docs/contracts/events/*.schema.json` | JSON Schemas for each SSE event type (`run.*`, `account.*`) |
| `backend/migrations/versions/0001–0006` | Alembic migrations: schema, RLS and grants, auth lookups, graph checkpoints, privacy purge, artifact content |
| `repositories/schema.py` | SQLAlchemy table metadata with retention comments |
| `cli/seed.py` | Seed data: Demo plan, model profiles, draft rate card v1, instrument catalogue |
| `adapters/fakes.py`, `adapters/fake_llm.py` | SYNTHETIC fixture data and a scenario-driven fake model (tests, e2e, offline eval; refused in production) |
| `.env` (never committed), `.env.example` | Runtime settings and secrets; the example holds placeholders only |
| `docker-compose.yml`, `docker-compose.e2e.yml`, `Caddyfile` | Local and e2e stacks; reverse proxy |
| `backend/Dockerfile`, `docker/postgres/init/01-roles.sh` | Backend image (api, worker, mcp); local role creation |
| `render.yaml`, `frontend/next.config.ts` | Render Blueprint; Vercel `/api` proxy |
| `docs/specs/*` | Requirements v2, Design v2 and the MVP build prompt |
| `docs/decisions.md`, `docs/deviations.md`, `docs/traceability.csv` | ADRs, deviations (DV-01 to DV-15), requirement-to-code mapping |
| `docs/ops/runbook.md`, `docs/ops/deploy-render-vercel.md` | Operations and deployment procedures |
| Artifacts volume `/app/data/artifacts` or `artifacts.content` | Raw provider responses and CSV exports (content-addressed, sha256-checked) |

## 8. Important database tables

| Table | Purpose | Written by |
| --- | --- | --- |
| `users`, `accounts`, `account_memberships` | Identity and tenancy; every member table carries `account_id` for RLS | `AuthService` |
| `sessions`, `auth_tokens` | Server-side sessions and one-time tokens (hashes only) | `AuthService` |
| `research_runs` | One row per run: status, billing status, snapshot, versions, timings | `ResearchService.admit`, `RunLifecycle` |
| `jobs`, `job_attempts` | Durable work queue with leases and fencing | `RunRepository`, `RecoverySweeper` |
| `run_nodes` | Per-node progress and outputs for resume | `RunHandle` |
| `llm_calls`, `usage_records` | Model call intents, outcomes, tokens and cost | `ModelCaller`, `UsageRepository` |
| `tool_calls`, `evidence_items`, `validation_results` | Tool calls, validated evidence, check results | `ToolCaller`, `EvidenceRepository` |
| `reports`, `report_citations` | Final reports and the evidence each claim cites | `EvidenceRepository.insert_report` |
| `run_events`, `account_events` | Replayable live events (SSE) | `append_run_event`, `append_account_event` |
| `wallets`, `reservations`, `ledger_transactions`, `ledger_postings` | Demo money: balance projection, holds, immutable double-entry journal | `WalletService`, `SettlementService` |
| `real_budget_accounts`, `real_spend_reservations` | Real USD spend caps per run, account-day, platform-day | `BudgetService` |
| `checkouts`, `payment_events`, `receipts`, `refunds` | Simulated payments | `BillingService` |
| `idempotency_records` | Stored responses for `Idempotency-Key` retries | `once()` |
| `conversations`, `messages`, `feedback` | History and ratings | `HistoryService` |
| `rate_cards`, `rate_card_items`, `model_profiles`, `instruments`, `provider_mappings`, `plans` | Configuration (read-only to the app roles) | Seed and admin CLI |
| `audit_events`, `security_events`, `incidents` | Audit trail, detections, operator alerts | `audit()`, `security_event()` |
| `deletion_requests`, `tombstones` | Scheduled deletions and their record | `AuthService`, `PurgeService` |
| `trace_outbox` | Buffered LangSmith trace exports | `LangSmithTraceSink` |
| `evaluation_runs`, `evaluation_scores` | Evaluation results | `EvalHarness` |

Access control: FORCE row-level security on member tables; three roles (`alloyiq_api`, `alloyiq_worker`, `alloyiq_migrator`); ledger postings and messages are protected by immutability triggers; journals must balance (deferred trigger).

## 9. Auditing

- **Audit trail (`audit_events`):** `repositories/audit.py` `audit()` records `created_at`, `account_id`, `actor_kind` (member, worker, admin CLI), `action` (e.g. `auth.login`, `run.admitted`, `run.settled`, `billing.checkout_simulated`, `account.suspend`), `outcome`, `target_type`, `target_id`, `run_id` and redacted `metadata`, in the **same transaction** as the change, so an action and its audit row commit or fail together.
- **Security detections (`security_events`):** `security_event()` records injection quarantines, abuse signals and session revocations.
- **Run-level record:** `run_events` and `run_nodes` timestamp every state change and node; `llm_calls`, `usage_records` and `tool_calls` record every external call with its outcome, including **unknown** outcomes rather than guesses.
- **Money integrity:** immutable, balanced ledger postings keyed by `business_event_id`; `alloyiq-admin reconcile payments` compares the wallet projection with postings and writes an `incidents` row on drift.
- **Privacy record:** `tombstones` record what account deletion removed, so a restore can re-apply it.
- **Traces:** `TraceSink` spans (redacted) go to LangSmith through the durable `trace_outbox`; failures set `observability_gap` on the run instead of blocking it.
- **Logs:** structured JSON with `request_id`, service and deployment version; secrets and personal data are never logged.
- **Reading the audit:** `alloyiq-admin audit --account <uuid> --action <prefix>`, `alloyiq-admin runs --account <uuid>`, `alloyiq-admin summary`. Output shows IDs and pseudonyms, never emails, prompts or report text.
