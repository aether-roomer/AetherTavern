#Requires -Version 5.1
# Restart the AetherTavern server: stops the previous instance recorded in
# the pidfile (if any) and launches a fresh uvicorn in its place.
$ErrorActionPreference = 'Stop'

Set-Location -Path $PSScriptRoot

function Get-EnvOrDefault([string]$Name, [string]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrEmpty($value)) { return $Default }
    return $value
}

$ServerHost = Get-EnvOrDefault 'AETHER_HOST' '127.0.0.1'
$ServerPort = Get-EnvOrDefault 'AETHER_PORT' '8000'
$DataDir    = Get-EnvOrDefault 'AETHER_DATA_DIR' 'data'
$PidFile    = Join-Path $DataDir 'server.pid'

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

# Stop the previous instance if its pidfile points at a live process tree.
# We only target what we previously launched, so unrelated processes that
# happen to bind the port are left alone. taskkill /T walks the child tree
# because `uv run python -m server` keeps the python interpreter and the
# uvicorn worker as descendants on Windows (no exec).
if (Test-Path $PidFile) {
    $oldPid = Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        & taskkill.exe /PID $oldPid /T 2>$null | Out-Null
        for ($i = 0; $i -lt 10; $i++) {
            if (-not (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 500
        }
        if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
            & taskkill.exe /F /PID $oldPid /T 2>$null | Out-Null
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

$launcherArgs = @('run', 'python', '-m', 'server', '--host', $ServerHost, '--port', $ServerPort)
$proc = Start-Process -FilePath 'uv' -ArgumentList $launcherArgs -NoNewWindow -PassThru
Set-Content -Path $PidFile -Value $proc.Id -Encoding ASCII

try {
    $proc.WaitForExit()
    exit $proc.ExitCode
}
finally {
    if (Test-Path $PidFile) {
        $current = Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($current -eq "$($proc.Id)") {
            Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        }
    }
}
