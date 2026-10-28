<#
.SYNOPSIS
  Migrate and seed configuration data (Demo plan, model profiles, draft rate card v1,
  reference instrument catalogue) as the alloyiq_migrator role. Safe to re-run.
.EXAMPLE
  scripts\seed.ps1
  scripts\seed.ps1 -Database alloyiq_test
#>
param([string]$Database = 'alloyiq')
. (Join-Path $PSScriptRoot '_common.ps1')
& (Join-Path $PSScriptRoot 'migrate.ps1') -Database $Database
$cfg = Read-EnvFile
$port = if ($cfg['POSTGRES_HOST_PORT']) { $cfg['POSTGRES_HOST_PORT'] } else { '5432' }
$env:ALEMBIC_DATABASE_URL = "postgresql+psycopg://alloyiq_migrator:$($cfg['ALLOYIQ_MIGRATOR_DB_PASSWORD'])@127.0.0.1:$port/$Database"
foreach ($k in 'DEMO_FX_USD_INR', 'DEMO_MARKUP', 'DEMO_DAILY_TOPUP_LIMIT_INR') { if ($cfg[$k]) { Set-Item "env:$k" $cfg[$k] } }
Push-Location (Join-Path $RepoRoot 'backend')
try { Write-Step "Seeding [$Database]"; Invoke-Native uv run python -m alloyiq.cli.seed } finally { Pop-Location }
