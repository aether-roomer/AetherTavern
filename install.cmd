@echo off
rem Double-clickable entry point for install.ps1.
rem
rem Windows opens .ps1 files in an editor when you double-click them instead
rem of running them, and a script that came out of a downloaded zip is refused
rem by the default execution policy. Going through PowerShell explicitly with
rem a per-process bypass sidesteps both, so this file works on a stock machine.
setlocal
set "missing="
if not exist "%~dp0install.ps1" set "missing=1"
if not exist "%~dp0windows-common.ps1" set "missing=1"
if defined missing (
    echo Could not find the AetherTavern scripts next to this file.
    echo Make sure you extracted the whole zip archive, not just this one file.
    pause
    exit /b 1
)
rem Prefer the absolute path. A PATH trimmed by policy, or one with another
rem powershell.exe ahead of the real one, would otherwise get to decide which
rem host runs our scripts. The bare name stays as a fallback so an unusual
rem Windows layout still gets a chance.
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
rem install.ps1 prompts on its own way out when setup worked, and stays quiet
rem when it didn't, so this is the only pause on the failing path. It also
rem catches the failures that never reach the script body -- a parse error, an
rem unmet #Requires, no PowerShell to be found at all -- where the window would
rem otherwise vanish with nothing left for the user to report.
set "rc=%errorlevel%"
if not "%rc%"=="0" pause
exit /b %rc%
