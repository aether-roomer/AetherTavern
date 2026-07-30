#Requires -Version 5.1
<#
    Helpers shared by install.ps1 and start.ps1. Dot-source it; there is
    nothing here to run on its own.
#>

# External programs write progress to stderr, and Windows PowerShell can
# surface that as a terminating error while ErrorActionPreference is 'Stop'.
# Run them relaxed and judge the outcome by the exit code instead.
function Invoke-External([scriptblock]$Action) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # A command that fails to *launch* never sets $LASTEXITCODE, so clear it
    # first -- otherwise a caller checking it reads the previous command's
    # success and calls a failure a win.
    $global:LASTEXITCODE = $null
    try { & $Action } finally { $ErrorActionPreference = $previous }
}

# Fold the persisted PATH into this session's copy. The uv installer writes
# the user PATH in the registry and broadcasts the change, but a process that
# is already running keeps the copy it started with -- which is why a fresh
# terminal is normally needed after installing uv. Entries already in
# $env:Path stay, and keep their precedence: some of them exist only in this
# process and would not survive a straight overwrite from the registry.
function Sync-PathFromRegistry {
    $entries = @()
    $sources = @(
        $env:Path,
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User')
    )
    foreach ($source in $sources) {
        foreach ($entry in ($source -split ';')) {
            if ($entry -and ($entries -notcontains $entry)) { $entries += $entry }
        }
    }
    $env:Path = $entries -join ';'
}

# Everywhere uv's own installer may have put the binary. The first block
# mirrors the dest_dir chain in https://astral.sh/uv/install.ps1, in its order
# of preference. The three force-install variables share one branch there and
# normally name the directory holding the binary outright, but the same branch
# appends bin/ under a hierarchical layout, so both spellings are checked.
# XDG_DATA_HOME really is '../bin' relative to the data dir, and the HOME entry
# is the PowerShell automatic $HOME rather than $env:HOME -- that is what the
# installer reads, and a redirected home directory can split it from
# $env:USERPROFILE. The .cargo entries and CARGO_HOME are not part of uv's
# chain; they find a uv that came from `cargo install` instead.
#
# Join-Path resolves the drive as it builds the path and errors when there is
# no such drive, which under our 'Stop' preference would abort the whole search
# over a single stale variable naming a disk nobody has plugged in. Ignore it:
# a directory we cannot even spell is not a candidate, and the filter below
# drops the empty result.
function Get-UvCandidateDirs {
    $forced = @(
        $env:UV_INSTALL_DIR
        $env:CARGO_DIST_FORCE_INSTALL_DIR
        $env:UV_UNMANAGED_INSTALL
    )
    $candidates = @(
        foreach ($dir in $forced) {
            if ($dir) { $dir; Join-Path $dir 'bin' -ErrorAction Ignore }
        }
        $env:XDG_BIN_HOME
        if ($env:XDG_DATA_HOME) { Join-Path $env:XDG_DATA_HOME '..\bin' -ErrorAction Ignore }
        if ($HOME)              { Join-Path $HOME '.local\bin' -ErrorAction Ignore }
        if ($env:CARGO_HOME)    { Join-Path $env:CARGO_HOME 'bin' -ErrorAction Ignore }
        if ($HOME)              { Join-Path $HOME '.cargo\bin' -ErrorAction Ignore }
        if ($env:USERPROFILE)   { Join-Path $env:USERPROFILE '.local\bin' -ErrorAction Ignore }
        if ($env:USERPROFILE)   { Join-Path $env:USERPROFILE '.cargo\bin' -ErrorAction Ignore }
    )
    return @($candidates | Where-Object { $_ } | Select-Object -Unique)
}

# Full path to uv.exe, or $null. Looks it up by name first; the directory
# sweep is for a uv that is installed but not yet on this process's PATH.
function Find-Uv {
    $cmd = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    foreach ($dir in Get-UvCandidateDirs) {
        # Same missing-drive story as in Get-UvCandidateDirs, and the forced
        # directories and XDG_BIN_HOME reach this point exactly as the
        # environment spelled them. Skip what Join-Path declines to build --
        # Test-Path refuses a null path outright.
        $candidate = Join-Path $dir 'uv.exe' -ErrorAction Ignore
        if (-not $candidate) { continue }
        if (Test-Path -LiteralPath $candidate) {
            # Present but not resolvable by name -- put its directory in front
            # for this session so plain 'uv' works from here on.
            if (($env:Path -split ';') -notcontains $dir) {
                $env:Path = "$dir;$env:Path"
            }
            return $candidate
        }
    }
    return $null
}

# Find-Uv, and if that draws a blank, again with the persisted PATH folded in.
# Recovers uv for a window that was already open when it was installed.
function Resolve-Uv {
    $uv = Find-Uv
    if ($uv) { return $uv }
    Sync-PathFromRegistry
    return Find-Uv
}
