<#
.SYNOPSIS
  Run the Playwright journeys against http://localhost:8080 (build prompt section 18).

  The stack is switched to a separate database (alloyiq_e2e) with SYNTHETIC providers and
  model output (docker-compose.e2e.yml: PROVIDER_MODE=fixtures, LLM_MODE=fake), so the
  journeys never spend real money. The dev stack is restored afterwards.
.EXAMPLE
  scripts\e2e.ps1
  scripts\e2e.ps1 -KeepE2EStack      # leave the synthetic stack running for debugging
#>
param([switch]$KeepE2EStack)
. (Join-Path $PSScriptRoot '_common.ps1')
Initialize-EnvFile
$cfg = Read-EnvFile
Set-Location $RepoRoot

Write-Step 'Preparing database alloyiq_e2e'
Invoke-Native docker compose up -d --wait postgres
$exists = docker compose exec -T postgres psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'alloyiq_e2e'"
if ("$exists".Trim() -ne '1') {
    Invoke-Native docker compose exec -T postgres psql -U postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE alloyiq_e2e OWNER alloyiq_migrator"
    Invoke-Native docker compose exec -T postgres psql -U postgres -v ON_ERROR_STOP=1 -c "REVOKE ALL ON DATABASE alloyiq_e2e FROM PUBLIC; GRANT CONNECT ON DATABASE alloyiq_e2e TO alloyiq_api, alloyiq_worker, alloyiq_migrator"
}
& (Join-Path $PSScriptRoot 'migrate.ps1') -Database alloyiq_e2e downgrade base *>&1 | Out-Null
& (Join-Path $PSScriptRoot 'seed.ps1') -Database alloyiq_e2e *>&1 | Out-Null
# Synthetic stack only: enable the reference instruments so journeys can run research.
# (catalog-verify refuses to enable instruments from fixture data, by design.)
Invoke-Native docker compose exec -T postgres psql -U postgres -d alloyiq_e2e -v ON_ERROR_STOP=1 -c "UPDATE instruments SET enabled = true, unavailable_reason = NULL WHERE canonical_id IN ('comex-gold-cont-ref','comex-silver-cont-ref','comex-copper-cont-ref')"

$failed = $false
try {
    Write-Step 'Starting the synthetic stack'
    Invoke-Native docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d --wait api worker mcp caddy web
    # A fresh dev server: on Windows bind mounts the file watcher can miss newly created files.
    Invoke-Native docker compose restart web
    Wait-HttpOk 'http://localhost:8080/api/v1/health/live' 180
    # Rate-limit buckets live in Redis, which holds nothing durable (ADR-005): start clean.
    Invoke-Native docker compose exec -T redis redis-cli FLUSHDB
    Wait-HttpOk 'http://localhost:8080/login' 300

    Push-Location (Join-Path $RepoRoot 'frontend')
    try {
        Write-Step 'Playwright journeys'
        $env:E2E_BASE_URL = 'http://localhost:8080'
        $env:MAILPIT_URL = 'http://localhost:8025'
        Invoke-Native pnpm exec playwright test
    } catch {
        $failed = $true
        Write-Host "    FAILED: $($_.Exception.Message)" -ForegroundColor Red
    } finally { Pop-Location }
} finally {
    if (-not $KeepE2EStack) {
        Write-Step 'Restoring the dev stack'
        Invoke-Native docker compose up -d api worker mcp
    }
}
if ($failed) { exit 1 }
Write-Host 'E2E journeys passed.' -ForegroundColor Green
