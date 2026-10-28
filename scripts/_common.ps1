# Shared helpers, dot-sourced by the other scripts. Windows PowerShell 5.1 compatible.
$ErrorActionPreference = 'Stop'
$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# Pick up tools installed since this shell started (uv, pnpm shims, Docker).
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')
$env:COREPACK_ENABLE_DOWNLOAD_PROMPT = '0'
$env:NEXT_TELEMETRY_DISABLED = '1'

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }

function Invoke-Native {
    # Runs a native command and throws if it fails (5.1 has no $PSNativeCommandUseErrorActionPreference).
    # Deliberately a simple function using $args: an advanced function (param + [Parameter]) gets
    # common parameters, so a native flag such as `-d` would be swallowed as `-Debug`.
    # Success is judged only by the exit code: when output is redirected, 5.1 turns every stderr
    # line (docker and pnpm write progress there) into an ErrorRecord, which 'Stop' would make fatal.
    $exe = $args[0]
    $rest = @($args | Select-Object -Skip 1)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $exe @rest } finally { $ErrorActionPreference = $saved }
    if ($LASTEXITCODE -ne 0) { throw "$exe $($rest -join ' ') failed with exit code $LASTEXITCODE" }
}

function New-Secret([int]$Bytes = 32) {
    $buf = New-Object byte[] $Bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    # URL-safe, no characters that need escaping in a connection URL.
    return ([Convert]::ToBase64String($buf)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Initialize-EnvFile {
    # Creates .env from .env.example once, replacing every change-me with a fresh random secret.
    $envPath = Join-Path $script:RepoRoot '.env'
    if (Test-Path $envPath) { return }
    Write-Step 'Creating .env from .env.example with generated secrets'
    $lines = Get-Content (Join-Path $script:RepoRoot '.env.example') | ForEach-Object {
        if ($_ -match '^([A-Z0-9_]+)=change-me(\s.*)?$') { "$($Matches[1])=$(New-Secret)$($Matches[2])" } else { $_ }
    }
    [System.IO.File]::WriteAllText($envPath, (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host '    .env created (gitignored). Add provider API keys there when a phase needs them.'
}

function Read-EnvFile {
    $values = @{}
    Get-Content (Join-Path $script:RepoRoot '.env') | ForEach-Object {
        if ($_ -match '^\s*([A-Z0-9_]+)=(.*)$') {
            $values[$Matches[1]] = ($Matches[2] -replace '\s+#.*$', '').Trim()
        }
    }
    return $values
}

function Wait-HttpOk([string]$Url, [int]$TimeoutSeconds = 180) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { return $r }
        } catch { Start-Sleep -Seconds 2 }
    }
    throw "Timed out after ${TimeoutSeconds}s waiting for $Url"
}
