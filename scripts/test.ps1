<#
.SYNOPSIS
  Run every automated check: backend (ruff, mypy, pytest), frontend (prettier,
  eslint, tsc, vitest) and security scans (pip-audit, pnpm audit, gitleaks).
  Integration tests use the compose PostgreSQL (database alloyiq_test), which
  this script starts if needed.
.EXAMPLE
  scripts\test.ps1
  scripts\test.ps1 -Only backend
  scripts\test.ps1 -SkipSecurity
#>
param(
    [ValidateSet('all', 'backend', 'frontend', 'security')][string]$Only = 'all',
    [switch]$SkipSecurity
)
. (Join-Path $PSScriptRoot '_common.ps1')
Set-Location $RepoRoot
$failures = New-Object System.Collections.Generic.List[string]

function Invoke-Check([string]$Name, [scriptblock]$Body) {
    Write-Step $Name
    try { & $Body } catch { Write-Host "    FAILED: $($_.Exception.Message)" -ForegroundColor Red; $failures.Add($Name) }
}

if ($Only -in 'all', 'backend') {
    Initialize-EnvFile
    $cfg = Read-EnvFile
    Invoke-Check 'Migrations down to base, back up, and seed (alloyiq_test)' {
        # Proves every downgrade works and gives each run a clean database.
        & (Join-Path $PSScriptRoot 'migrate.ps1') -Database alloyiq_test downgrade base *>&1 | Out-Null
        if (-not $?) { throw 'downgrade failed' }
        & (Join-Path $PSScriptRoot 'seed.ps1') -Database alloyiq_test *>&1 | Out-Null
        if (-not $?) { throw 'upgrade/seed failed' }
    }
    $port = if ($cfg['POSTGRES_HOST_PORT']) { $cfg['POSTGRES_HOST_PORT'] } else { '5432' }
    $h = "127.0.0.1:$port/alloyiq_test"
    $env:TEST_DATABASE_URL = "postgresql+asyncpg://alloyiq_api:$($cfg['ALLOYIQ_API_DB_PASSWORD'])@$h"
    $env:TEST_WORKER_DATABASE_URL = "postgresql+asyncpg://alloyiq_worker:$($cfg['ALLOYIQ_WORKER_DB_PASSWORD'])@$h"
    $env:TEST_MIGRATOR_DATABASE_URL = "postgresql+psycopg://alloyiq_migrator:$($cfg['ALLOYIQ_MIGRATOR_DB_PASSWORD'])@$h"
    $env:LIVE_PROVIDER_TESTS = '0'   # never spend real money from the default suite

    Push-Location (Join-Path $RepoRoot 'backend')
    try {
        Invoke-Check 'backend: uv sync' { Invoke-Native uv sync --frozen }
        Invoke-Check 'backend: ruff format --check' { Invoke-Native uv run ruff format --check src tests migrations }
        Invoke-Check 'backend: ruff check' { Invoke-Native uv run ruff check src tests migrations }
        Invoke-Check 'backend: mypy' { Invoke-Native uv run mypy }
        Invoke-Check 'backend: pytest' { Invoke-Native uv run pytest }
    } finally { Pop-Location }
}

if ($Only -in 'all', 'frontend') {
    Push-Location (Join-Path $RepoRoot 'frontend')
    try {
        Invoke-Check 'frontend: pnpm install' { Invoke-Native pnpm install --frozen-lockfile }
        Invoke-Check 'frontend: prettier --check' { Invoke-Native pnpm format:check }
        Invoke-Check 'frontend: eslint' { Invoke-Native pnpm lint }
        Invoke-Check 'frontend: typecheck' { Invoke-Native pnpm typecheck }
        Invoke-Check 'frontend: vitest' { Invoke-Native pnpm test }
    } finally { Pop-Location }
}

if ($Only -in 'all', 'backend', 'frontend') {
    Invoke-Check 'contracts: committed OpenAPI, event schemas and TS types match the code' {
        & (Join-Path $PSScriptRoot 'gen-contracts.ps1') -Check *>&1 | Out-Host
        if (-not $?) { throw 'contracts out of date' }
    }
}

if (-not $SkipSecurity -and $Only -in 'all', 'security') {
    Invoke-Check 'security: pip-audit (locked backend deps)' {
        $req = Join-Path $env:TEMP 'alloyiq-requirements.txt'
        Push-Location (Join-Path $RepoRoot 'backend')
        try { Invoke-Native uv export --frozen --all-groups --no-emit-project --no-hashes -o $req -q } finally { Pop-Location }
        # The export is the complete pinned tree, so audit it as-is without resolving again.
        Invoke-Native uvx pip-audit --strict --no-deps --disable-pip -r $req
    }
    Invoke-Check 'security: pnpm audit' {
        Push-Location (Join-Path $RepoRoot 'frontend')
        try { Invoke-Native pnpm audit --audit-level moderate } finally { Pop-Location }
    }
    Invoke-Check 'security: gitleaks (history + staged + unstaged changes)' {
        $repo = "${RepoRoot}:/repo"
        $gl = @('run', '--rm', '-v', $repo, '-e', 'GIT_CONFIG_COUNT=1', '-e', 'GIT_CONFIG_KEY_0=safe.directory',
                '-e', 'GIT_CONFIG_VALUE_0=*', 'zricethezav/gitleaks:latest')
        Invoke-Native docker @($gl + @('git', '/repo', '--no-banner', '--redact'))
        Invoke-Native docker @($gl + @('git', '/repo', '--pre-commit', '--staged', '--no-banner', '--redact'))
        Invoke-Native docker @($gl + @('git', '/repo', '--pre-commit', '--no-banner', '--redact'))
    }
}

Write-Host ''
if ($failures.Count -gt 0) {
    Write-Host "FAILED ($($failures.Count)): $($failures -join '; ')" -ForegroundColor Red
    exit 1
}
Write-Host 'All checks passed.' -ForegroundColor Green
