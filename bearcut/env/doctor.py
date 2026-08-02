# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""環境自檢。

兩種消費者，所以輸出要有兩種形態：
- **人**：看 `render()` 的中文表格，每個沒過的項目都直接附上怎麼修
- **AI Agent**：吃 `check()` 回的 dict（`cli.py doctor --json`），自己判斷要不要先跑 bootstrap

只准用標準函式庫——doctor 必須在「什麼都還沒裝好」的壞掉環境裡也能跑，
不然使用者卡住時就沒有工具可以診斷了。
"""

import importlib.util
import os
import shutil
import unicodedata
from pathlib import Path

from . import ffmpeg as ff
from .platform import ROOT, summary, venv_python

# P1 之後才會真的用到，這裡先偵測，讓使用者提早知道缺什麼
_PY_DEPS = ["faster_whisper", "funasr", "torch", "PIL", "jieba"]

# 判斷腦：本地 CLI 或 API 金鑰，有一個就能跑語意判斷
_LLM_CLIS = ["claude", "codex", "gemini"]
_LLM_KEYS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"]


def _deps_status() -> dict:
    """檢查相依套件在**目前這個直譯器**裡是否可 import。"""
    got, missing = [], []
    for m in _PY_DEPS:
        (got if importlib.util.find_spec(m) else missing).append(m)
    return {"ok": not missing, "installed": got, "missing": missing}


def _llm_install_cmd(os_name: str) -> str:
    """裝一顆本機判斷腦的官方指令（依平台）。"""
    if os_name == "windows":
        return "irm https://claude.ai/install.ps1 | iex"      # PowerShell
    return "curl -fsSL https://claude.ai/install.sh | bash"   # macOS / Linux / WSL


def _llm_fix(os_name: str) -> str:
    """判斷腦缺席時的下一步。

    刻意寫成「可以直接貼上執行的指令」而不是「請安裝 Claude Code」——這句話最常
    出現在**客戶端的 agent**（Cowork、Codex…）眼前，它要的是能立刻執行的東西。

    為什麼優先推 CLI 而不是 API 金鑰：客戶若已在用 Claude 的付費方案，裝完
    `claude` 用同一個帳號登入就有判斷腦，不必再申請金鑰、不必再付一次錢。
    """
    shell = "PowerShell" if os_name == "windows" else "終端機"
    return (f"缺一顆判斷腦。在{shell}執行 `{_llm_install_cmd(os_name)}`，"
            "再執行一次 `claude` 用你現有的 Claude 付費方案登入（免 API 金鑰），"
            "然後重跑 doctor。"
            "或改設定 ANTHROPIC_API_KEY／OPENAI_API_KEY／GEMINI_API_KEY 環境變數。")


def _llm_status() -> dict:
    """問 bearcut.llm 本身，而不是自己再猜一次——偵測邏輯只該有一份。"""
    try:
        from ..llm import detect_all, get_llm
    except Exception:
        # llm 子套件壞掉不該讓 doctor 整個掛掉，doctor 正是用來診斷壞掉的環境的
        return {"ok": False, "backends": [], "selected": None,
                "note": "判斷腦模組載入失敗"}

    found = detect_all()
    usable = [p.describe() for p, ok in found if ok]
    selected = get_llm(refresh=True)
    return {
        "ok": selected.available(),
        "backends": [{"name": p.name, "kind": p.kind, "available": ok}
                     for p, ok in found],
        "selected": selected.name if selected.available() else None,
        "usable": usable,
        "note": (f"使用 {selected.describe()}" if selected.available() else
                 "沒有判斷腦：只能剪靜音，語意判斷（重講/口吃/校字）會停用"
                 "（正在操作 BearCut 的 AI agent 不算——判斷腦必須是程式呼叫得到的 CLI 或 API）"),
    }


def check() -> dict:
    """跑完整自檢，回結構化結果。"""
    plat = summary()
    fmpg = {
        "ok": ff.ready(),
        "ffmpeg": str(ff.find("ffmpeg") or ""),
        "ffprobe": str(ff.find("ffprobe") or ""),
        "version": ff.version("ffmpeg"),
        "vendored": bool(ff.vendored("ffmpeg")),
    }
    deps = _deps_status()
    llm = _llm_status()

    checks = {
        "python": {
            "ok": plat["python_ok"],
            "detail": f"Python {plat['python']}",
            "fix": f"需要 Python {plat['python_min']} 以上，請至 python.org 更新",
        },
        "venv": {
            "ok": plat["venv_exists"],
            "detail": "已建立" if plat["venv_exists"] else "尚未建立",
            "fix": "執行 python bootstrap.py",
        },
        "ffmpeg": {
            "ok": fmpg["ok"],
            "detail": fmpg["version"] or "找不到",
            "fix": "執行 python bootstrap.py（會自動下載，不會動到系統設定）",
        },
        "deps": {
            "ok": deps["ok"],
            "detail": (f"{len(deps['installed'])}/{len(_PY_DEPS)} 已安裝"
                       + (f"，缺：{', '.join(deps['missing'])}" if deps["missing"] else "")),
            "fix": "執行 python bootstrap.py",
        },
        "llm": {
            "ok": llm["ok"],
            "detail": llm["note"],
            "fix": _llm_fix(plat["os"]),
            # 給 agent 直接執行用，不必自己從 fix 那句話裡剖字串
            "fix_command": _llm_install_cmd(plat["os"]),
        },
    }

    # llm 缺席只降級不擋路，所以不列入致命項
    blocking = [k for k, v in checks.items() if not v["ok"] and k != "llm"]

    # 目前只有 Windows 經過完整驗證。其他平台的程式碼路徑都寫好了，
    # 但沒實機跑過就是沒驗證過——與其讓使用者以為壞了，不如先講清楚。
    warnings = []
    if plat["os"] != "windows":
        warnings.append(
            f"{plat['os']} 目前是實驗性支援：程式碼路徑已具備，但尚未在實機驗證，"
            "可能遇到問題，效能也可能較低。歡迎回報結果。")
    if plat["gpu"]["kind"] != "cuda":
        # 「有卡但 CUDA 不完整」跟「真的沒卡」是兩件事，給同一句話會讓有顯卡的
        # 使用者以為 BearCut 認不得他的卡（gpu() 早就回了 gpu_present，這裡要用）
        present = plat["gpu"].get("gpu_present")
        if present:
            warnings.append(
                f"偵測到 {present}，但 CUDA 函式庫不完整，辨識仍會走 CPU。"
                "執行 python bootstrap.py 會自動裝 CUDA 版 PyTorch 修好它。")
        else:
            warnings.append("沒有 NVIDIA GPU，辨識會走 CPU，速度明顯較慢（功能不受影響）。")

    return {
        "ok": not blocking,
        "blocking": blocking,
        "warnings": warnings,
        "supported_platform": plat["os"] == "windows",
        "checks": checks,
        "platform": plat,
        "ffmpeg": fmpg,
        "deps": deps,
        "llm": llm,
        "root": str(ROOT),
    }


_LABEL = {"python": "Python", "venv": "虛擬環境", "ffmpeg": "FFmpeg",
          "deps": "相依套件", "llm": "判斷腦"}


def _w(s: str) -> int:
    """字串的終端顯示寬度。中日韓字佔兩欄，用字元數對齊會歪掉。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _w(s))


def render(res: dict) -> str:
    """人看的報告。"""
    p = res["platform"]
    g = p["gpu"]
    lines = [
        "",
        "  BearCut 環境檢查",
        "  " + "─" * 52,
        f"  系統      {p['os']} / {p['arch']}" + ("（Apple Silicon）" if p["apple_silicon"] else ""),
        f"  加速      {g['detail']}",
        f"  專案位置  {res['root']}",
        "  " + "─" * 52,
    ]
    for key, c in res["checks"].items():
        mark = "✓" if c["ok"] else ("!" if key == "llm" else "✗")
        lines.append(f"   {mark}  {_pad(_LABEL[key], 12)}{c['detail']}")
        if not c["ok"]:
            lines.append(f"        └ {c['fix']}")
    lines.append("  " + "─" * 52)
    if res["ok"]:
        lines.append("  全部就緒，可以開始剪片。")
    else:
        lines.append(f"  還缺 {len(res['blocking'])} 項，照上面的指示補齊即可。")
    for w in res.get("warnings", []):
        lines.append(f"  ⚠ {w}")
    lines.append("")
    return "\n".join(lines)
