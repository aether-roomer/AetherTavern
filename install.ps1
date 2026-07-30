#Requires -Version 5.1
<#
    One-shot Windows setup for AetherTavern.

    Installs uv (which brings its own Python), makes it usable in this same
    session, clears the "downloaded from the internet" flag off the launcher
    scripts, and materialises the virtualenv.

    Safe to re-run: every step checks its own precondition first.
#>

$ErrorActionPreference = 'Stop'

$Common = Join-Path $PSScriptRoot 'windows-common.ps1'
if (-not (Test-Path -LiteralPath $Common)) {
    Write-Host 'Could not find windows-common.ps1 next to this file.' -ForegroundColor Red
    Write-Host 'Make sure you extracted the whole zip archive.'
    exit 1
}
. $Common

# PowerShell 5.1 on older Windows builds still negotiates TLS 1.0 by default,
# which modern download hosts refuse.
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

$UvInstallUrl = 'https://astral.sh/uv/install.ps1'

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "    $Message" -ForegroundColor Green
}

# Fetch and run uv's own installer, in a child PowerShell rather than through
# Invoke-Expression. The installer ends in a bare `exit 1` on failure, and
# `exit` inside an Invoke-Expression takes the calling script down with it --
# setup would stop dead here, past our try/catch, with none of our own
# reporting and the dependency step silently skipped. A child process contains
# that and hands back an exit code we can act on. It goes via a file because
# the installer is ~22 KB, close enough to the command-line length limit to be
# worth not testing.
function Install-Uv {
    $scratch = Join-Path ([System.IO.Path]::GetTempPath()) `
        ('uv-install-' + [guid]::NewGuid().ToString('n') + '.ps1')
    try {
        # Straight to disk: downloading into a variable first would put the
        # response through content-type interpretation and a re-encode on the
        # way back out, neither of which a script file benefits from. The
        # timeout is generous for a ~22 KB file, and there so that a route that
        # swallows the connection fails with something to read rather than
        # leaving setup apparently running forever.
        Invoke-WebRequest -Uri $UvInstallUrl -UseBasicParsing -OutFile $scratch `
            -TimeoutSec 120
        # Run it under the host that is running us, rather than whatever
        # 'powershell.exe' happens to resolve to on PATH. Both switches below
        # mean the same thing to Windows PowerShell and to pwsh, so either host
        # can carry the installer.
        $psExe = $null
        try { $psExe = (Get-Process -Id $PID).Path } catch { }
        if (-not $psExe) { $psExe = 'powershell.exe' }
        Invoke-External {
            & $psExe -NoProfile -ExecutionPolicy Bypass -File $scratch
        }
        if ($LASTEXITCODE -ne 0) {
            throw "The uv installer failed with exit code $LASTEXITCODE."
        }
    } finally {
        Remove-Item -LiteralPath $scratch -Force -ErrorAction SilentlyContinue
    }

    Sync-PathFromRegistry
    $uv = Find-Uv
    if (-not $uv) {
        throw ('uv was installed but could not be found afterwards. ' +
               'Close this window, open a new one, and run install.cmd again.')
    }
    return $uv
}

function Invoke-Setup {
    Set-Location -LiteralPath $PSScriptRoot

    Write-Host ''
    Write-Host 'AetherTavern setup' -ForegroundColor White
    Write-Host "Folder: $PSScriptRoot"

    # --- 0. Are we where we think we are? ------------------------------------
    foreach ($required in 'pyproject.toml', 'uv.lock') {
        if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $required))) {
            throw ("$required is missing from this folder. Extract the whole " +
                   'zip archive, then run install.cmd from the folder that has ' +
                   'README.md in it.')
        }
    }

    # --- 1. Clear the internet-source flag -----------------------------------
    # Files extracted from a browser-downloaded zip are marked as coming from
    # the internet, and a RemoteSigned policy refuses to run them on that mark
    # alone. Our own launchers are unaffected either way -- the
    # -ExecutionPolicy Bypass in install.cmd / start.cmd ignores the zone
    # outright -- but a user who instead relaxes the policy for their account,
    # the route the README describes, does need the mark gone.
    Write-Step 'Unblocking the scripts in this folder'
    $scripts = @(Get-ChildItem -LiteralPath $PSScriptRoot -File |
        Where-Object { $_.Extension -in '.ps1', '.cmd', '.bat' })
    foreach ($file in $scripts) {
        try { Unblock-File -LiteralPath $file.FullName } catch { }
    }
    Write-Ok "$($scripts.Count) script(s) checked."

    # --- 2. Execution policy (reported, not changed) -------------------------
    # An installer relaxing this is the obvious move, and it's the wrong one:
    # the policy covers every script the account ever runs, not just this
    # project's, so it isn't ours to widen. start.cmd passes
    # -ExecutionPolicy Bypass for its own process, which is all we need.
    #
    # A bare Get-ExecutionPolicy would answer the wrong question: it reports
    # the effective policy, Process scope outranks the persistent ones, and
    # install.cmd launched us with Process set to Bypass. The answer would
    # describe this window every time and the account never. Walking the
    # persistent scopes in precedence order, Process left out, is what tells
    # the user whether start.ps1 would run on its own.
    Write-Step 'Checking the PowerShell execution policy'
    $effective = $null
    foreach ($scope in 'MachinePolicy', 'UserPolicy', 'CurrentUser', 'LocalMachine') {
        $policy = Get-ExecutionPolicy -Scope $scope
        if ($policy -ne 'Undefined') { $effective = $policy.ToString(); break }
    }
    if ($effective) {
        Write-Ok "Currently '$effective' - left as it is."
        if ($effective -in 'Restricted', 'AllSigned') {
            Write-Host '    Your account blocks PowerShell scripts, so start AetherTavern'
            Write-Host '    with start.cmd - it bypasses that for itself only, and'
            Write-Host '    changes nothing for anything else on your machine.'
        }
    } else {
        # Nothing is persisted, so a built-in default decides, and it is not the
        # same one everywhere: Restricted on client Windows, RemoteSigned on
        # Server. Naming either would be wrong on the other, and the advice that
        # follows holds regardless of which one this machine has.
        Write-Ok 'Not configured - the Windows default applies. Left as it is.'
        Write-Host '    If that default blocks scripts, start AetherTavern with'
        Write-Host '    start.cmd - it bypasses the policy for itself only, and'
        Write-Host '    changes nothing for anything else on your machine.'
    }

    # --- 3. uv ---------------------------------------------------------------
    Write-Step 'Looking for uv'
    $uv = Resolve-Uv
    if ($uv) {
        Write-Ok "Found: $uv"
    } else {
        Write-Host '    Not installed. Downloading the official installer...'
        $uv = Install-Uv
        Write-Ok "Installed: $uv"
    }

    # An executable that is present but cannot run -- an interrupted download,
    # a build for the wrong architecture -- would otherwise print its error
    # message where the version belongs and fail two steps later, where the
    # message on offer blames the lockfile.
    $version = (@(Invoke-External { & $uv --version 2>&1 }) -join ' ').Trim()
    if ($LASTEXITCODE -ne 0) {
        throw ("'uv --version' failed with exit code $LASTEXITCODE, so the uv at " +
               "$uv is not runnable. Delete it and run install.cmd again to " +
               "reinstall. It said: $version")
    }
    Write-Ok $version

    # --- 4. Dependencies -----------------------------------------------------
    Write-Step 'Installing dependencies (this can take a few minutes the first time)'
    Invoke-External { & $uv sync --locked }
    if ($LASTEXITCODE -ne 0) {
        throw ("'uv sync --locked' failed with exit code $LASTEXITCODE. " +
               "If your uv is older than this project's lockfile, run " +
               "'uv self update' and try again.")
    }
    Write-Ok 'Dependencies installed.'
}

try {
    Invoke-Setup
} catch {
    Write-Host ''
    Write-Host 'Setup failed.' -ForegroundColor Red
    Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ''
    Write-Host 'If this keeps happening, copy the text above when asking for help.'
    # No prompt here: install.cmd pauses on a non-zero exit, and asking the
    # user to dismiss the same window twice reads like the first one failed.
    exit 1
}

Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host ''
Write-Host 'To start AetherTavern, double-click start.cmd in this folder.'
Write-Host 'The first start downloads the GLM-4.6 tokenizer (about 20 MB).'
Write-Host 'Then open http://127.0.0.1:8000 in your browser.'
Write-Host ''
Read-Host 'Press Enter to close this window' | Out-Null
