@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title BearCut
cd /d "%~dp0"

rem ============================================================
rem  THIS FILE MUST STAY PURE ASCII. Do not put Chinese here.
rem
rem  Why: cmd.exe parses a .bat using the SYSTEM codepage (cp950 on
rem  Traditional Chinese Windows), not the console codepage. "chcp 65001"
rem  fixes display but NOT parsing. When a UTF-8 Chinese character's
rem  second byte happens to be 0x26 (&) or 0x7C (|), cmd splits the line
rem  there and tries to run the tail as a command -- the user sees
rem  "'...' is not recognized" on the very first thing they ever do.
rem  All Chinese lives in assets\messages\*.txt and is printed with
rem  "type", which does not go through cmd's command parser.
rem
rem  Two batch traps this file has already been bitten by -- do not
rem  "simplify" them back:
rem   1. %errorlevel% inside a ( ) block expands at PARSE time, so it is
rem      empty. Use !errorlevel! (delayed expansion) or "if errorlevel N".
rem   2. "if not defined X" inside a FOR body also expands at parse time,
rem      so a loop that sets X and checks it in the same body never works.
rem      Hence the flat, repeated blocks below instead of a loop.
rem ============================================================

set "MSG=assets\messages"

echo.
echo   BearCut
echo   (c) Brightstream Technology
echo   ----------------------------------------------------
echo.

rem ---- Find a Python that actually RUNS ----------------------------
rem  Being on PATH is not enough. Stock Windows ships 0-byte "app
rem  execution alias" stubs at %LOCALAPPDATA%\Microsoft\WindowsApps\.
rem  "where" finds them, but running one opens the Microsoft Store and
rem  exits non-zero WITHOUT PRINTING ANYTHING -- which is exactly the
rem  blank failure the first customer hit: banner, nothing, "setup did
rem  not finish". So every candidate is smoke-tested before it is used.

set "PY="

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3"

if not defined PY (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    python3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python3"
)

if not defined PY (
    rem Separate "no Python at all" from "only the Store stub is there":
    rem the fix differs -- install it, vs turn the alias off.
    set "STUB="
    if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe" set "STUB=1"
    if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\py.exe" set "STUB=1"
    if defined STUB (
        if exist "%MSG%\python_store_stub.txt" (
            type "%MSG%\python_store_stub.txt"
        ) else (
            echo   Python is not really installed -- only the Microsoft Store
            echo   placeholder is present. Install Python from python.org.
        )
    ) else (
        if exist "%MSG%\no_python.txt" (
            type "%MSG%\no_python.txt"
        ) else (
            echo   Python not found. Install Python 3.9+ from python.org
            echo   and tick "Add python.exe to PATH" during setup.
        )
    )
    echo.
    pause
    start "" "https://www.python.org/downloads/"
    exit /b 1
)

rem ---- First run: install ------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    if exist "%MSG%\first_run.txt" type "%MSG%\first_run.txt"
    echo.
    %PY% bootstrap.py
    rem Capture the code IMMEDIATELY. Any command in between -- even the
    rem "type" below, which succeeds -- resets errorlevel to 0, and we would
    rem print "exit code 0" for a run that actually failed. (Been there.)
    set "RC=!errorlevel!"
    if not "!RC!"=="0" (
        echo.
        if exist "%MSG%\install_failed.txt" (
            type "%MSG%\install_failed.txt"
        ) else (
            echo   Setup did not finish. See the messages above.
        )
        rem Always show the code and which interpreter ran. A silent
        rem failure with no number is a failure nobody can report.
        echo.
        echo   [exit code !RC! / python: %PY%]
        echo.
        pause
        exit /b 1
    )
)

if exist "%MSG%\starting.txt" type "%MSG%\starting.txt"
echo.
".venv\Scripts\python.exe" cli.py ui

echo.
if exist "%MSG%\stopped.txt" type "%MSG%\stopped.txt"
pause
