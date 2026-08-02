@echo off
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
rem  "'...' is not recognized as an internal or external command"
rem  on the very first thing they ever do with BearCut.
rem
rem  All Chinese text lives in assets\messages\*.txt (UTF-8) and is
rem  printed with "type", which emits raw bytes the console renders
rem  correctly. Everything after Python starts is printed by Python,
rem  which handles UTF-8 properly (see bearcut/env/platform.py).
rem ============================================================

set "MSG=assets\messages"

echo.
echo   BearCut
echo   (c) Brightstream Technology
echo   ----------------------------------------------------
echo.

rem Find Python. The official Windows installer ships the "py" launcher,
rem prefer it so we get 3.x; fall back to "python" on PATH.
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)

if not defined PY (
    if exist "%MSG%\no_python.txt" (
        type "%MSG%\no_python.txt"
    ) else (
        echo   Python not found. Please install Python 3.9+ from python.org
        echo   and tick "Add python.exe to PATH" during setup.
    )
    echo.
    pause
    start "" "https://www.python.org/downloads/"
    exit /b 1
)

rem First run has no .venv -- install. Afterwards skip straight to the UI.
if not exist ".venv\Scripts\python.exe" (
    if exist "%MSG%\first_run.txt" type "%MSG%\first_run.txt"
    echo.
    %PY% bootstrap.py
    if errorlevel 1 (
        echo.
        if exist "%MSG%\install_failed.txt" (
            type "%MSG%\install_failed.txt"
        ) else (
            echo   Setup did not finish. See the messages above.
        )
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
