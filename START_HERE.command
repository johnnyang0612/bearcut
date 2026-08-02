#!/bin/bash
# BearCut — macOS 啟動器
# © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
#
# 註：從 Finder 下載後首次使用，可能需要先在這個檔案上按右鍵 → 開啟，
# 或執行 chmod +x START_HERE.command，macOS 才允許執行。

cd "$(dirname "$0")" || exit 1

echo
echo "  BearCut 自動順剪"
echo "  © 熊董 × 川輝科技 Brightstream Technology"
echo "  ----------------------------------------------------"
echo "  ⚠  macOS 目前是實驗性支援"
echo "     程式碼路徑已具備，相依套件與 FFmpeg 也確認可安裝，"
echo "     但尚未在實機完整驗證，可能遇到問題、效能也可能較低。"
echo "     遇到狀況歡迎到 GitHub 開 issue 回報，幫我們把它變成正式支援。"
echo "  ----------------------------------------------------"
echo

# macOS 內建的 /usr/bin/python3 是 Xcode 命令列工具的殼，未安裝時呼叫會跳安裝視窗。
# 依序找真正可用的直譯器：Homebrew → python3 → python
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        if "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)' 2>/dev/null; then
            PY="$c"; break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "  找不到 Python 3.9 以上版本，這是執行 BearCut 的必要元件。"
    echo
    echo "  最快的安裝方式（擇一）："
    echo "    A. 到 https://www.python.org/downloads/ 下載安裝"
    echo "    B. 若已安裝 Homebrew，執行：brew install python"
    echo
    echo "  裝完後回來重新開啟這個檔案。"
    echo
    read -r -p "  按 Enter 關閉..."
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "  第一次執行，開始安裝所需元件。"
    echo "  會下載約 2-3 GB，時間依網速而定，請不要關閉視窗。"
    echo
    if ! "$PY" bootstrap.py; then
        echo
        echo "  安裝沒有完成，請看上面的訊息。"
        echo "  多數情況重跑一次就會好；若持續失敗請把畫面截圖回報。"
        echo
        read -r -p "  按 Enter 關閉..."
        exit 1
    fi
fi

echo
echo "  啟動中，瀏覽器會自動打開..."
echo "  （關掉這個視窗就會停止）"
echo
.venv/bin/python cli.py ui

echo
echo "  已停止。"
read -r -p "  按 Enter 關閉..."
