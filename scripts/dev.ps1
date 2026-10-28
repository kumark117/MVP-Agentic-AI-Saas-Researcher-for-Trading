<#
.SYNOPSIS
  Start, stop and inspect the AlloyIQ dev stack.
.EXAMPLE
  scripts\dev.ps1            # build + start everything, wait until healthy through Caddy
  scripts\dev.ps1 -HostWeb   # same, but run Next.js on Windows (faster reload; ADR-008)
  scripts\dev.ps1 down       # stop (keeps data volumes)
  scripts\dev.ps1 reset      # stop and DELETE all data volumes
  scripts\dev.ps1 logs api   # follow logs of one service
  scripts\dev.ps1 status
#>
param(
    [ValidateSet('up', 'down', 'reset', 'logs', 'status')][string]$Command = 'up',
    [string]$Service,
    [switch]$HostWeb
)
. (Join-Path $PSScriptRoot '_common.ps1')
Set-Location $RepoRoot

switch ($Command) {
    'up' {
        Initialize-EnvFile
        if ($HostWeb) {
            $env:WEB_UPSTREAM = 'host.docker.internal:3000'
            Write-Step 'Starting backend services (web runs on the host)'
            Invoke-Native docker compose up -d --build caddy api worker mcp postgres redis mailpit
            docker compose stop web | Out-Null   # in case an earlier run started it
        } else {
            $env:WEB_UPSTREAM = 'web:3000'
            Write-Step 'Building and starting all services'
            Invoke-Native docker compose up -d --build
        }
        Write-Step 'Waiting for the API through Caddy'
        $r = Wait-HttpOk 'http://localhost:8080/api/v1/health/live'
        Write-Host "    $($r.Content)"
        if ($HostWeb) {
            Write-Step 'Starting Next.js on the host (Ctrl+C stops it; the containers keep running)'
            Set-Location (Join-Path $RepoRoot 'frontend')
            Invoke-Native pnpm install --frozen-lockfile
            pnpm dev --port 3000
        } else {
            Write-Step 'Waiting for the web app through Caddy (first compile can take a minute)'
            Wait-HttpOk 'http://localhost:8080/' 300 | Out-Null
            Write-Host ''
            Write-Host '    App      http://localhost:8080'
            Write-Host '    Mailpit  http://localhost:8025'
        }
    }
    'down'   { Invoke-Native docker compose down }
    'reset'  { Invoke-Native docker compose down --volumes --remove-orphans }
    'logs'   { if ($Service) { docker compose logs -f --tail 200 $Service } else { docker compose logs -f --tail 100 } }
    'status' { Invoke-Native docker compose ps }
}
