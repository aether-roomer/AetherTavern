#Requires -Version 5.1
# Restart the AetherTavern server: stops the previous instance recorded in
# the pidfile (if any) and launches a fresh uvicorn in its place.
$ErrorActionPreference = 'Stop'

# Taken before anything else runs. $args is an automatic that every invocation
# rebinds, including the dot-source below, which runs in this very scope; and
# once we are inside a function it means that function's arguments instead.
$ScriptArgs = @($args)

Set-Location -LiteralPath $PSScriptRoot

$Common = Join-Path $PSScriptRoot 'windows-common.ps1'
if (-not (Test-Path -LiteralPath $Common)) {
    Write-Host 'Could not find windows-common.ps1 next to this file.' -ForegroundColor Red
    Write-Host 'Make sure you extracted the whole zip archive.'
    exit 1
}
. $Common

function Get-EnvOrDefault([string]$Name, [string]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrEmpty($value)) { return $Default }
    return $value
}

# PowerShell 5.1's Start-Process joins -ArgumentList with single spaces and
# quotes nothing, so an argument holding whitespace has to arrive quoted --
# otherwise `--proxy-rules C:\Users\Some One\rules.yaml` reaches the launcher
# as two arguments. Quoting follows the rules the C runtime uses to split a
# command line back apart: a backslash is only special in front of a quote,
# so runs of them there, and at the end where they would otherwise escape our
# own closing quote, have to be doubled.
function Format-Argument([string]$Value) {
    if ($Value -eq '') { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $escaped = $Value -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

# Resolve the pidfile to a live process, but only while it still looks like the
# launcher we started. Windows reissues pids quickly, so a pidfile left behind
# by an unclean exit -- window closed with the X, reboot, power cut -- can name
# an unrelated process by the time we read it, and taskkill /T would take that
# process down along with its whole child tree. Two things must agree before we
# touch anything:
#
#   * the image is the uv we launch with;
#   * it started no later than the moment we recorded the pid. We write the
#     pidfile immediately after launching, so ours always satisfies that, while
#     a reissued pid belongs to a process that started after ours had died.
function Get-TrackedProcess([string]$PidFilePath, [string]$ExpectedName) {
    if (-not (Test-Path -LiteralPath $PidFilePath)) { return $null }

    # An interrupted write can leave anything at all in here, and neither of the
    # ways that bites is something -ErrorAction can soften: a non-numeric -Id is
    # a parameter-binding failure, and a run of digits too long for an Int32
    # throws on the cast. Either would escape this function and abort the start
    # over a file we are only consulting, so parse it rather than convert it.
    $recorded = Get-Content -LiteralPath $PidFilePath -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $recordedPid = 0
    if (-not [int]::TryParse([string]$recorded, [ref]$recordedPid)) { return $null }
    if ($recordedPid -le 0) { return $null }

    $proc = Get-Process -Id $recordedPid -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }
    if ($proc.ProcessName -ne $ExpectedName) { return $null }

    # StartTime throws for processes this account does not own. Ours it does,
    # so a failure here is one more sign the pid has moved on.
    try { $startedAt = $proc.StartTime } catch { return $null }
    # The slack absorbs coarse filesystem timestamps: FAT32 stores mtime to the
    # nearest two seconds.
    $recordedAt = (Get-Item -LiteralPath $PidFilePath).LastWriteTime.AddSeconds(5)
    if ($startedAt -gt $recordedAt) { return $null }

    return $proc
}

# Every descendant of $RootPid, breadth-first, as id + image name. Recording
# the name matters because the list is consumed after things have started
# dying, by which point a bare pid no longer identifies anything.
#
# Walking ParentProcessId can in principle reach a process that merely
# inherited a reissued pid somewhere up the chain. taskkill /T walks the same
# links and takes the same chance, so nothing here is broader than the kill it
# is checking up on.
function Get-DescendantProcesses([int]$RootPid) {
    $all = @(Get-CimInstance -ClassName Win32_Process `
        -Property ProcessId, ParentProcessId, Name -ErrorAction SilentlyContinue)

    $byParent = @{}
    foreach ($entry in $all) {
        $parent = [int]$entry.ParentProcessId
        if (-not $byParent.ContainsKey($parent)) { $byParent[$parent] = @() }
        $byParent[$parent] += $entry
    }

    $found = @()
    $seen = @{}
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($RootPid)
    while ($queue.Count -gt 0) {
        $current = [int]$queue.Dequeue()
        # Pid 0 parents itself, and a reissued pid can close a longer loop.
        if ($seen.ContainsKey($current)) { continue }
        $seen[$current] = $true
        foreach ($child in $byParent[$current]) {
            $found += [pscustomobject]@{
                Id   = [int]$child.ProcessId
                Name = [System.IO.Path]::GetFileNameWithoutExtension($child.Name)
            }
            $queue.Enqueue([int]$child.ProcessId)
        }
    }
    return $found
}

# Force-kill $TargetPid, if it still looks like the process we recorded, and say
# what came of it: 'skipped' when there was nothing of ours to stop, 'stopped'
# once it is confirmed gone, 'failed' when it outlived the attempt. taskkill's
# exit code answers a different question -- it reports 0 once the kernel has
# accepted the request rather than once the process has left the table, and it
# fails outright on a process this account cannot touch -- so the process list
# is the only thing that can tell us whether the kill landed.
function Stop-TrackedProcess([int]$TargetPid, [string]$ExpectedName, [datetime]$Before) {
    $survivor = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
    if (-not $survivor -or $survivor.ProcessName -ne $ExpectedName) { return 'skipped' }
    try { if ($survivor.StartTime -gt $Before) { return 'skipped' } } catch { return 'skipped' }

    Invoke-External { & taskkill.exe /F /PID $TargetPid /T 2>$null | Out-Null }
    for ($i = 0; $i -lt 10; $i++) {
        $still = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
        if (-not $still -or $still.ProcessName -ne $ExpectedName) { return 'stopped' }
        Start-Sleep -Milliseconds 200
    }
    return 'failed'
}

function Stop-PreviousServer([string]$PidFilePath, [string]$ExpectedName) {
    $previous = Get-TrackedProcess $PidFilePath $ExpectedName
    if (-not $previous) { return }
    $oldPid = $previous.Id

    # taskkill /T walks the child tree because `uv run python -m server` keeps
    # the python interpreter and the uvicorn worker as descendants on Windows
    # (no exec). Take the tree down on paper first: once uv is gone so are the
    # parent links, and a descendant that outlives the sweep goes on holding
    # the port, which pushes the replacement onto 8001 without telling anyone.
    $descendants = @(Get-DescendantProcesses $oldPid | Where-Object { $_ })
    $startedBefore = Get-Date

    # The kill is forced because nothing gentler can reach this process at all.
    # Unforced, taskkill asks by posting WM_CLOSE to the target's windows; we
    # launch with -NoNewWindow, so the server owns none, and the request comes
    # back refused without the server ever having heard it. Reaching it properly
    # would mean starting it in its own process group and signalling that with
    # GenerateConsoleCtrlEvent, which buys less than it costs: every write the
    # server makes is atomic, so a kill mid-flight loses the write in progress
    # and leaves nothing half-written behind it.
    if ((Stop-TrackedProcess $oldPid $ExpectedName $startedBefore) -eq 'failed') {
        Write-Host "Could not stop the previous server (pid $oldPid). It may still be holding the port." -ForegroundColor Yellow
    }
    foreach ($entry in $descendants) {
        switch (Stop-TrackedProcess $entry.Id $entry.Name $startedBefore) {
            'stopped' {
                Write-Host "Stopped a leftover $($entry.Name) process (pid $($entry.Id)) from the previous run."
            }
            'failed' {
                Write-Host "Could not stop a leftover $($entry.Name) process (pid $($entry.Id)). It may still be holding the port." -ForegroundColor Yellow
            }
        }
    }
}

function Start-Server {
    $serverHost = Get-EnvOrDefault 'AETHER_HOST' '127.0.0.1'
    $serverPort = Get-EnvOrDefault 'AETHER_PORT' '8000'
    $dataDir    = Get-EnvOrDefault 'AETHER_DATA_DIR' 'data'

    # Anchor a relative data dir to the project folder. Set-Location moves
    # PowerShell's own location but not the process working directory that .NET
    # sees, so the two disagree whenever this script is invoked by absolute path
    # from somewhere else.
    if (-not [System.IO.Path]::IsPathRooted($dataDir)) {
        $dataDir = Join-Path $PSScriptRoot $dataDir
    }
    $pidFile = Join-Path $dataDir 'server.pid'

    [void][System.IO.Directory]::CreateDirectory($dataDir)

    # Before stopping anything: a uv we cannot find would leave the user with
    # the old server killed and no new one, which is worse than never having
    # touched it.
    $uv = Resolve-Uv
    if (-not $uv) {
        Write-Host ''
        Write-Host 'Could not find uv.' -ForegroundColor Red
        Write-Host 'AetherTavern needs it to run. Double-click install.cmd in this'
        Write-Host 'folder to install it, then try start.cmd again.'
        return 1
    }

    Stop-PreviousServer $pidFile ([System.IO.Path]::GetFileNameWithoutExtension($uv))
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue

    # Extra arguments go on to `python -m server` (--no-evade-used-port,
    # --proxy-rules, ...). argparse honours the last occurrence of an option, so
    # passing --port or --host here overrides the environment defaults above.
    $launcherArgs = @('run', 'python', '-m', 'server', '--host', $serverHost, '--port', $serverPort) +
        $ScriptArgs
    $commandLine = ($launcherArgs | ForEach-Object { Format-Argument $_ }) -join ' '
    $proc = Start-Process -FilePath $uv -ArgumentList $commandLine `
        -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru

    # Reading Handle while the process is alive is what keeps it open, and
    # PowerShell 7.4 onwards no longer does that for us. Without it ExitCode
    # reads back as $null once the process is gone, because the kernel has
    # nothing left to answer from. A server that failed during startup can be
    # gone before we get this far, and whatever it printed on its way out is a
    # better account of that than a failure to read its handle would be.
    try { $null = $proc.Handle } catch { }

    # A pidfile we cannot write costs us the ability to stop this server from
    # the next start.cmd. Killing a server that is otherwise working fine over
    # that is the worse trade, so say what was lost and carry on.
    $tracked = $true
    try {
        Set-Content -LiteralPath $pidFile -Value $proc.Id -Encoding ASCII
    } catch {
        $tracked = $false
        Write-Host ''
        Write-Host "Could not write $pidFile - $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host 'The server is starting anyway, but the next start.cmd will not be'
        Write-Host 'able to stop it. Close this window to stop it instead.'
        Write-Host ''
    }

    try {
        $proc.WaitForExit()
        # Whatever the exit code turns out to be, it is not worth reporting a
        # failed start over: by this point the server has been and gone. Treat
        # an unreadable one as a clean exit.
        $code = 0
        try {
            if ($null -ne $proc.ExitCode) { $code = [int]$proc.ExitCode }
        } catch { }
        return $code
    }
    finally {
        if ($tracked -and (Test-Path -LiteralPath $pidFile)) {
            $current = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($current -eq "$($proc.Id)") {
                Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

try {
    exit (Start-Server)
} catch {
    Write-Host ''
    Write-Host 'AetherTavern could not start.' -ForegroundColor Red
    Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ''
    Write-Host 'If this keeps happening, copy the text above when asking for help.'
    # start.cmd pauses on a non-zero exit, which is what keeps this on screen
    # when the window was opened by double-clicking it.
    exit 1
}
