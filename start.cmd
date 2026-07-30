@echo off
rem Double-clickable entry point for start.ps1 - see install.cmd for why the
rem detour through PowerShell is needed. Leave the window that opens running:
rem closing it stops AetherTavern.
setlocal
set "missing="
if not exist "%~dp0start.ps1" set "missing=1"
if not exist "%~dp0windows-common.ps1" set "missing=1"
if defined missing (
    echo Could not find the AetherTavern scripts next to this file.
    echo Make sure you extracted the whole zip archive, not just this one file.
    pause
    exit /b 1
)
rem Absolute path with a fallback to the bare name - see install.cmd.
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "rc=%errorlevel%"
rem Ctrl+C in this window is the other way to stop the server, and a console
rem process killed that way exits with STATUS_CONTROL_C_EXIT. Pausing on it
rem would ask the user to dismiss a window they had just shut down on purpose.
if "%rc%"=="-1073741510" set "rc=0"
if not "%rc%"=="0" pause
exit /b %rc%
