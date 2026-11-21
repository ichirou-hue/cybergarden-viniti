@echo off
rem ===========================================================================
rem  One-command launcher for the VINITI backend (Windows).
rem
rem  Usage:
rem      run_server.bat                       default port 8765, data\viniti.sqlite3
rem      run_server.bat -Port 8813            another port
rem      run_server.bat -Data data\demo.sqlite3
rem      run_server.bat -Check -NoPause       preflight only, no server, no prompt
rem      run_server.bat --help
rem
rem  All real logic lives in run_server.ps1: it picks the Python interpreter,
rem  runs passport.py --check, db.py --init --stats and then server.py in this
rem  very window. This file is ASCII-only on purpose: Windows CMD misreads
rem  non-ASCII batch files, so the Russian messages are printed by PowerShell.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "%~dp0run_server.ps1" (
    echo [ERROR] run_server.ps1 is missing next to run_server.bat.
    exit /b 2
)

set "PS_EXE="
for %%P in (powershell.exe pwsh.exe) do (
    if not defined PS_EXE (
        where %%P >nul 2>&1 && set "PS_EXE=%%P"
    )
)
if not defined PS_EXE (
    echo [ERROR] PowerShell not found in PATH ^(powershell.exe or pwsh.exe^).
    echo [ERROR] See README.md for the manual start commands.
    exit /b 2
)

rem Keep the window open when the file was started by double click.
rem In that case cmd.exe runs as: cmd /c ""...\run_server.bat" "
rem and %cmdcmdline% contains the script name. From an open console it does not.
set "PAUSE_ARG="
echo %cmdcmdline% | findstr /i /c:"%~nx0" >nul 2>&1
if not errorlevel 1 set "PAUSE_ARG=-PauseOnExit"

"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_server.ps1" %* %PAUSE_ARG%
exit /b %ERRORLEVEL%
