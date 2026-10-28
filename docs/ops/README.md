# Operations notes

The MVP runs as a single-host Docker Compose stack. Production operations are **future work**
(deviation DV-06). Nothing here claims an SLO, capacity figure or recovery objective has been
met.

## Deferred to a later hardening phase (R059-R063)

| Area | Spec target | Needed before it can be claimed |
|---|---|---|
| Availability | 99.9% monthly for eligible requests, measured at the gateway | Hosting choice (OD-5), at least 2 API replicas, gateway SLI metrics |
| Latency | Reads p95 < 300 ms / p99 < 800 ms; admission p95 < 700 ms; queue p95 < 5 s; research p95 < 60 s / p99 < 120 s | Load test at the capacity envelope with real provider quotas |
| Capacity | 1,000 members, 200 concurrent users, 100 executions, 50 RPS, 500 SSE | Same load test |
| Recovery | RPO 5 min, RTO 60 min, verified quarterly | Managed Postgres with PITR, restore drill that includes artifacts, usage and demo-ledger reconciliation and tombstone replay |
| Alerting | Burn rate, queue age, worker heartbeat, spend caps, unsettled usage, ledger mismatch, stale data, trace backlog | Metrics backend and on-call owner (OD-8) |

A short runbook (`runbook.md`) arrives in P9.

## Local stack quick reference

```powershell
scripts\dev.ps1            # start everything; waits until healthy through Caddy
scripts\dev.ps1 -HostWeb   # backend in containers, Next.js on Windows (ADR-008)
scripts\dev.ps1 status
scripts\dev.ps1 logs api
scripts\dev.ps1 down       # keep data
scripts\dev.ps1 reset      # DELETE data volumes (re-runs the Postgres role bootstrap)
```

The Postgres role bootstrap (`docker/postgres/init/01-roles.sh`) runs only when the `pg_data`
volume is first created. If you change a database password in `.env`, run `dev.ps1 reset`.
