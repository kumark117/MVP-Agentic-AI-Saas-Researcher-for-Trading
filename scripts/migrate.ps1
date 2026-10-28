<#
.SYNOPSIS
  Run Alembic as the alloyiq_migrator role against the compose PostgreSQL.
.EXAMPLE
  scripts\migrate.ps1                      # upgrade head (database alloyiq)
  scripts\migrate.ps1 -Database alloyiq_test
  scripts\migrate.ps1 downgrade base
  scripts\migrate.ps1 revision --autogenerate -m "add x"
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Database = 'alloyiq',
    [Parameter(ValueFromRemainingArguments)][string[]]$AlembicArgs
)
. (Join-Path $PSScriptRoot '_common.ps1')
Initialize-EnvFile
$cfg = Read-EnvFile
$port = if ($cfg['POSTGRES_HOST_PORT']) { $cfg['POSTGRES_HOST_PORT'] } else { '5432' }
if (-not $AlembicArgs -or $AlembicArgs.Count -eq 0) { $AlembicArgs = @('upgrade', 'head') }

Push-Location $RepoRoot
try { Invoke-Native docker compose up -d --wait postgres } finally { Pop-Location }

$env:ALEMBIC_DATABASE_URL = "postgresql+psycopg://alloyiq_migrator:$($cfg['ALLOYIQ_MIGRATOR_DB_PASSWORD'])@127.0.0.1:$port/$Database"
Push-Location (Join-Path $RepoRoot 'backend')
try {
    Write-Step "alembic $($AlembicArgs -join ' ')  [$Database]"
    Invoke-Native uv run alembic @AlembicArgs
} finally { Pop-Location }
