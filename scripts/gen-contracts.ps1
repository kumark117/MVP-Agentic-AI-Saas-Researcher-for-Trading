<#
.SYNOPSIS
  Regenerate the committed contracts (D110) and the frontend API types:
    docs/contracts/openapi.json, docs/contracts/events/*.schema.json,
    frontend/src/lib/api/schema.d.ts
  CI (scripts/test.ps1) fails if the committed files differ from what the code generates.
.EXAMPLE
  scripts\gen-contracts.ps1
  scripts\gen-contracts.ps1 -Check
#>
param([switch]$Check)
. (Join-Path $PSScriptRoot '_common.ps1')
$contracts = Join-Path $RepoRoot 'docs\contracts'
$types = Join-Path $RepoRoot 'frontend\src\lib\api\schema.d.ts'

Push-Location (Join-Path $RepoRoot 'backend')
try {
    if ($Check) { Invoke-Native uv run python -m alloyiq.api.contracts --check $contracts }
    else { Invoke-Native uv run python -m alloyiq.api.contracts $contracts }
} finally { Pop-Location }

Push-Location (Join-Path $RepoRoot 'frontend')
try {
    if ($Check) {
        # Inside frontend/ so the project's Prettier config applies (gitignored).
        $tmp = Join-Path $RepoRoot 'frontend\src\lib\api\.schema.check.d.ts'
        Invoke-Native pnpm exec openapi-typescript (Join-Path $contracts 'openapi.json') -o $tmp
        Invoke-Native pnpm exec prettier --write $tmp --log-level warn
        $same = (Get-Content $tmp -Raw) -eq (Get-Content $types -Raw)
        Remove-Item $tmp -Force
        if (-not $same) { throw "$types is out of date; run scripts\gen-contracts.ps1" }
    } else {
        Invoke-Native pnpm exec openapi-typescript (Join-Path $contracts 'openapi.json') -o $types
        Invoke-Native pnpm exec prettier --write $types --log-level warn
    }
} finally { Pop-Location }
Write-Step $(if ($Check) { 'Contracts are up to date' } else { 'Contracts regenerated' })
