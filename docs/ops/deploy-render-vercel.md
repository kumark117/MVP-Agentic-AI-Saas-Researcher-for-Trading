# Deploy: frontend on Vercel (Hobby), backend on Render

```
browser ──► https://<project>.vercel.app ──(/api/* rewrite)──► https://alloyiq-api.onrender.com
                 Next.js (Vercel)                                 FastAPI (Render Starter web)
                                                                  worker + MCP tools (Render Starter, paid)
                                                                  Postgres 16 (Render free)
                                                                  Key Value (Render free)
```

The browser only talks to the Vercel origin; Vercel forwards `/api/*` to Render, so session
cookies stay first-party. What differs from the local stack is recorded as DV-15.

## Costs and limits (check current pricing before you start)

| Part | Plan | Notes |
|---|---|---|
| Frontend | Vercel Hobby (free) | Personal, non-commercial use only |
| API | Render Starter (paid, about USD 7/month) | Always on. On the free plan it sleeps after 15 min idle and the next visitor waits about a minute or more (seen in practice: the page sits on "Loading your account...") |
| Worker | Render Starter (paid, about USD 7/month) | Always on; also runs the MCP tool server on localhost |
| Postgres | Render free | 1 GB; **expires 30 days after creation** (14-day grace) unless upgraded |
| Key Value | Render free | In-memory only, which is fine (ADR-005) |
| Email | Gmail SMTP | Needs a Gmail **app password**; low daily sending limits |
| Claude, Alpha Vantage, Tavily | Your keys | Real spend, capped by `REAL_SPEND_*` |

## 1. Gmail app password

1. Turn on 2-Step Verification for the Google account.
2. Open <https://myaccount.google.com/apppasswords>, create one named "AlloyIQ", and copy the
   16-character password. You'll paste it into Render as `SMTP_PASSWORD`.

## 2. Vercel (frontend)

1. <https://vercel.com/new>, sign in with GitHub, import the repository.
2. **Root Directory:** `frontend`. Framework: Next.js (detected).
3. **Environment variables:**
   - `API_ORIGIN` = `https://alloyiq-api.onrender.com` (the Render API URL from step 3; if the
     name is taken, Render shows the real URL; update this and redeploy)
   - `ENABLE_EXPERIMENTAL_COREPACK` = `1` (uses the pinned pnpm version)
4. Deploy and note the URL, e.g. `https://alloyiq-xyz.vercel.app`. This is `PUBLIC_ORIGIN`.

## 3. Render (backend)

1. <https://dashboard.render.com> → **New → Blueprint**, connect GitHub, pick the repository.
   Render reads `render.yaml` and lists `alloyiq-api`, `alloyiq-worker`, `alloyiq-cache` and
   `alloyiq-db`.
2. Fill in the secrets it asks for (never commit them):

   | Variable | Value |
   |---|---|
   | `PUBLIC_ORIGIN` (api and worker) | The Vercel URL from step 2, `https://...`, no trailing slash |
   | `SMTP_USERNAME`, `EMAIL_FROM` | Your Gmail address |
   | `SMTP_PASSWORD` | The Gmail app password |
   | `ANTHROPIC_API_KEY`, `ALPHAVANTAGE_API_KEY`, `TAVILY_API_KEY` | Your keys |
   | `LANGSMITH_API_KEY` | Optional; leave empty to disable tracing |
   | `ENABLE_YAHOO_REFERENCE` | `1` to use Yahoo reference data (unlicensed, dev only, ADR-006; instruments mapped to Yahoo need it), else `0` |

3. Apply. The worker's pre-deploy step runs `python -m alloyiq.cli.render predeploy`: it
   creates the `alloyiq_api`, `alloyiq_worker` and `alloyiq_migrator` roles, runs the
   migrations and seeds the catalogue, plans, model profiles and draft rate card. It is safe to
   re-run on every deploy. The worker then starts with `python -m alloyiq.cli.render worker`,
   which launches the MCP tool server on `127.0.0.1:8100` and, once it is healthy, the worker.
   If either exits, both stop and Render restarts the container.

## 4. One-time activation (Render worker → Shell)

```sh
LIVE_PROVIDER_TESTS=1 python -m alloyiq.cli.render exec -- alloyiq-admin catalog-verify --live --apply
python -m alloyiq.cli.render exec -- alloyiq-admin rate-card-activate v1-2026-10-06 --confirm-prices-verified
python -m alloyiq.cli.render exec -- alloyiq-admin eval
```

Every admin command on Render goes through `python -m alloyiq.cli.render exec --`, which
builds the role database URLs from the Blueprint's variables.

## 5. Check

1. Open the Vercel URL, register, and find the verification email in your inbox (or spam).
2. Run one research run and watch the live progress.
3. `alloyiq-admin summary` from the worker shell shows real spend against the caps.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 403 `CSRF_FAILED` on every form | `PUBLIC_ORIGIN` doesn't exactly match the Vercel URL |
| Pages load, every API call fails with 404 or 502 | `API_ORIGIN` is wrong on Vercel, or the API is still waking up |
| No verification email | Wrong app password, or 2-Step Verification is off; see the worker logs |
| `PUBLIC_ORIGIN must be an https:// origin` at start | It's set to `http://` |
| Database disappears after about 30 days | Render free Postgres expired; upgrade it before then |
| Live progress stream reconnects during a run | Expected through the proxy: the client resumes from its last event |
| Worker logs `mcp did not become ready` | The MCP server failed to start; its error is in the lines just above |
