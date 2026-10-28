# AlloyIQ

An AI agent SaaS for metals-trading **research**. Price, News and Technical agents gather evidence
in parallel, and a Signal agent writes a cited report (BUY / SELL / HOLD / NO_SIGNAL).

- **Research only.** No broker orders and no investment capital.
- **Payments are simulated.** Demo credit in INR has no cash value; money collected is always zero.
- **Model and data costs are real**, measured, and capped by spend limits that demo credit cannot raise.

Specs: `docs/specs/` (Requirements v2 R001-R074, Design v2, and the MVP build prompt).
Status: **MVP build complete (P0-P9)**, pending the owner's live checks: catalogue verification
with real data keys, rate-card price verification, and one live research run. See
`docs/traceability.csv`, `docs/deviations.md`, `docs/decisions.md` and `docs/ops/runbook.md`.

## Prerequisites (Windows 11)

Docker Desktop (WSL 2), Git, Node.js 24 LTS with pnpm (via corepack), and uv (it manages Python 3.12).

## Run

Windows blocks `.ps1` scripts by default. Allow local scripts once for your user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

(or run each script as `powershell -ExecutionPolicy Bypass -File scripts\dev.ps1`).

```powershell
scripts\dev.ps1          # first run creates .env with random secrets, builds, starts, waits
```

| URL | What |
|---|---|
| http://localhost:8080 | The app: the only origin the browser uses |
| http://localhost:8080/api/v1/health/live | API liveness |
| http://localhost:8080/api/v1/docs | Interactive API docs (dev only) |
| http://localhost:8025 | Mailpit (dev email) |

`docker compose up` also works once `.env` exists. Use `scripts\dev.ps1 -HostWeb` to run Next.js on
Windows for faster reloads (ADR-008).

## Test

```powershell
scripts\test.ps1                 # backend + frontend + security scans
scripts\test.ps1 -Only backend
scripts\test.ps1 -SkipSecurity
```

```powershell
scripts\e2e.ps1                  # Playwright journeys on a separate SYNTHETIC stack (no cost)
docker compose exec api alloyiq-admin eval      # 37-case evaluation, offline and synthetic
```

Tests never call paid providers unless `LIVE_PROVIDER_TESTS=1` is set and the owner agrees.

## Layout

```
backend/    FastAPI api, worker and mcp (one package: src/alloyiq), uv + pytest
frontend/   Next.js App Router + TypeScript, pnpm + Vitest
docker/     container bootstrap (Postgres roles)
docs/       specs, ADRs (decisions.md), deviations, traceability, contracts, ops
scripts/    PowerShell helpers: dev, test, migrate, gen-contracts, seed, e2e, extract-specs
```

Architecture rules: domain code has no I/O; route handlers only call services; no route touches the
ledger or a model directly; agents cannot run SQL, touch payments or write signals.

## Authors

- **Kumar Krishnamoorthy**: product owner; specifications, decisions and live verification
- **Claude & Team** (Claude Opus 5.5 by Anthropic, with Claude Code): design and implementation

Every phase commit carries a `Co-Authored-By: Claude Opus 5.5` trailer.
