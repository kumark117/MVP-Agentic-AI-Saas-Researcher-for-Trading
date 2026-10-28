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
