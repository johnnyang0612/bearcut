@echo off
chcp 65001 >nul 2>&1
title BearCut - 自動順剪
cd /d "%~dp0"

echo.
echo   BearCut 自動順剪
echo   (c) 熊董 x 川輝科技 Brightstream Technology
echo   ----------------------------------------------------
echo.

rem 找 Python。Windows 官方安裝器會裝 py 啟動器，優先用它挑 3.x；
rem 沒有再退回 python。兩個都沒有就引導使用者去安裝，不要丟錯誤碼給人看。
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)

if not defined PY (
    echo   找不到 Python，這是執行 BearCut 的必要元件。
    echo.
    echo   請照以下步驟做，大約三分鐘：
    echo     1. 按 Enter 會自動開啟 Python 官方下載頁
    echo     2. 下載並執行安裝檔
    echo     3. 安裝時務必勾選 "Add python.exe to PATH"
    echo     4. 裝完後回來重新雙擊這個檔案
    echo.
    pause
    start "" "https://www.python.org/downloads/"
    exit /b 1
)

rem 首次執行沒有 .venv，跑安裝；之後就跳過直接開 UI
if not exist ".venv\Scripts\python.exe" (
    echo   第一次執行，開始安裝所需元件。
    echo   會下載約 2-3 GB，時間依網速而定，請不要關閉視窗。
    echo.
    %PY% bootstrap.py
    if errorlevel 1 (
        echo.
        echo   安裝沒有完成，請看上面的訊息。
        echo   多數情況重跑一次就會好；若持續失敗請把畫面截圖回報。
        echo.
        pause
        exit /b 1
    )
)

echo.
echo   啟動中，瀏覽器會自動打開...
echo   （關掉這個視窗就會停止）
echo.
".venv\Scripts\python.exe" cli.py ui

echo.
echo   已停止。
pause
