# AlloyIQ runbook (MVP, single host)

Short, practical procedures for the owner. The MVP runs one Docker Compose stack; nothing
here claims an SLO or recovery objective has been met (DV-06). Admin commands run on the
host or in the api container:

```powershell
docker compose exec api alloyiq-admin <command>     # uses DATABASE_MIGRATOR_URL
```

## Daily checks

| Check | How | Healthy |
|---|---|---|
| Stack up | `scripts\dev.ps1 status` | all services Up / healthy |
| Platform summary | `alloyiq-admin summary` | no open incidents; platform spend well under `REAL_SPEND_PLATFORM_DAY_USD` |
| Demo ledger | `alloyiq-admin reconcile payments` | `problems: []` (exit 0) |
| Queue | `alloyiq-admin summary` → `runs_by_status.queued` | small; queued runs time out after 5 min by design |
| Evaluation | `alloyiq-admin eval` (offline, synthetic) | `release_gates_passed: true` |

## Owner digest (sign-ups and runs)

With `OWNER_ALERT_EMAIL` set, the worker emails an hourly digest of new sign-ups and finished
research runs (member email, instrument, horizon, result, real cost, duration; never questions
or report text). Quiet hours send nothing. Each covered window is recorded in `audit_events`
(`owner_digest.sent` / `owner_digest.empty`), which is also where the next digest starts, so a
worker outage delays activity but never skips it: `alloyiq-admin audit --action owner_digest`.

## Provider outage (Claude, Alpha Vantage, Tavily, Yahoo)

Symptoms: runs end `incomplete` with NO_SIGNAL, or `failed` with `PROVIDER_UNAVAILABLE`;
tool failures in run activity. Nothing to repair: runs never fabricate data, and unused
reservations are released at settlement. If one provider is down for long:

1. Disable affected instruments so members see the reason instead of failed runs:
   `alloyiq-admin catalog-verify --live --apply` (needs `LIVE_PROVIDER_TESTS=1`; it
   disables instruments whose checks fail and records why).
2. For Claude: runs fall back only to approved models on refusal or overload. If Claude is
   unreachable, unset `ANTHROPIC_API_KEY` and restart the worker; new runs then fail fast
   with "Research is unavailable" instead of waiting on timeouts.

## Real-spend limits reached

`503 BUDGET_EXCEEDED` at admission means a daily real-spend cap is full. Caps reset at
00:00 UTC. Raising a cap is an owner decision (OD-2): edit `REAL_SPEND_*` in `.env`, then
`docker compose up -d api worker`. Demo credit can never raise them.

## Stuck or failed runs

- A worker crash is recovered automatically: leases expire after 30 s, the sweeper
  requeues within 15 s, and completed provider calls are reused (no double charge).
- A run is failed with an incident after 3 attempts. Inspect it:
  `alloyiq-admin runs --account <uuid>` and `alloyiq-admin audit --account <uuid>`.
- Unknown usage keeps a bounded pending hold for 24 h, then the scheduler releases it and
  the platform absorbs the cost.

## Database

- Migrate: `scripts\migrate.ps1` (as `alloyiq_migrator`). Every migration has a working
  downgrade; `scripts\test.ps1` proves down-and-up on `alloyiq_test`.
- Backup (manual in the MVP):
  `docker compose exec postgres pg_dump -U postgres -Fc alloyiq > backup.dump`.
  A backup does not include artifacts (the `artifacts` volume); copy that too.
- Restore: `pg_restore -U postgres -d alloyiq --clean backup.dump`, then re-apply
  deletions recorded in `tombstones` (rows created after the backup). Restore drills are
  future work (DV-06).
- If Docker Desktop restarts and the host can no longer reach Postgres or
  `localhost:8080` while the containers report healthy, the port forwarding is stale:
  `docker compose restart postgres caddy`.

## Redis outage

Redis holds only rate-limit buckets and caches (ADR-005). If it is down, rate limiting
falls back to a stricter in-process limiter and caches miss; research, billing and SSE
replay keep working from PostgreSQL. Restart it with `docker compose restart redis`.

## Billing questions (simulated)

- A member's checkout is stuck: `pending` checkouts expire after 30 minutes; nothing is
  credited unless a success event is applied.
- Goodwill credit: `alloyiq-admin grant-credit --account <uuid> --amount 100.00
  --reason "..." --event-id <unique>` (audited; re-running with the same event ID is a
  no-op).
- Projection drift: `alloyiq-admin reconcile payments --repair` rebuilds the wallet from
  postings and writes an incident.

## Security events

- Injection attempts are quarantined automatically (`security_events`,
  `injection.quarantined`); no action needed unless one source repeats.
- Suspected account abuse: `alloyiq-admin suspend <account> --reason "..."` ends its
  sessions and blocks new research; `--lift` reverses it.
- Key compromise: rotate the provider key in its console, update `.env`, restart
  api, worker and mcp. Rotating `SESSION_SECRET` signs every member out and invalidates
  CSRF tokens and export links.

## Account deletion

Requests are scheduled 7 days ahead. The hourly purge then revokes sessions, cancels
queued work, deletes conversations, messages, feedback, reports, evidence, activity and
exports, pseudonymises the user and writes tombstones. Demo ledger and provider-cost
records are kept under their own retention. LangSmith-side deletion is manual (DV-10).

## Rate card

Rate card v1 is a draft until the owner checks every price against Anthropic's pricing
page: `alloyiq-admin rate-card-activate v1-2026-10-06` lists them, and
`--confirm-prices-verified` activates it. Active cards are immutable.
