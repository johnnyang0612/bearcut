# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""平台偵測。

**這支檔案只准用標準函式庫。** `bootstrap.py` 會在什麼套件都還沒裝的情況下 import 它，
多一個第三方相依就會讓「乾淨機器上雙擊就能裝」這件事整個垮掉。
"""

import os
import platform as _platform
import shutil
import subprocess
import sys
from pathlib import Path

# --- 專案根目錄 ---------------------------------------------------------------
# 這支檔案在 <root>/bearcut/env/platform.py，往上三層就是 root。
ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor"          # 自動下載的 ffmpeg / 模型落點（git-ignored）
VENV = ROOT / ".venv"

MIN_PYTHON = (3, 9)


def os_name() -> str:
    """回傳 'windows' / 'macos' / 'linux'。"""
    s = sys.platform
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "macos"
    return "linux"


def arch() -> str:
    """回傳 'x86_64' / 'arm64' / 原始字串。

    注意 Windows 的 ARM 會回 'arm64'，macOS 的 Intel 會回 'x86_64'——
    ffmpeg 下載要靠這個挑對版本。
    """
    m = _platform.machine().lower()
    if m in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m


def is_apple_silicon() -> bool:
    return os_name() == "macos" and arch() == "arm64"


def python_ok() -> bool:
    return sys.version_info[:2] >= MIN_PYTHON


def python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def venv_python() -> Path:
    """venv 裡的 python 執行檔路徑（不保證存在）。"""
    return VENV / ("Scripts/python.exe" if os_name() == "windows" else "bin/python")


def in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _cuda_usable() -> bool:
    """CUDA 是不是**真的能用**，而不只是「有顯示卡」。

    這兩件事經常不一致：PyPI 上的 torch 在 Windows 預設是 CPU 版，不帶 CUDA 函式庫，
    所以機器有 RTX 顯卡、nvidia-smi 也跑得動，實際推論時卻會因為找不到 cuBLAS 而失敗。
    誠實回報比樂觀回報重要——不然使用者會以為自己有 GPU 加速，卻納悶為什麼這麼慢。
    """
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def gpu() -> dict:
    """偵測可用的加速器。回 `{"kind": "cuda"|"mps"|"cpu", "detail": str}`。"""
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run(
                [smi, "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=10,
            )
            names = (out.stdout or "").strip().splitlines()
            if out.returncode == 0 and names:
                name = names[0].strip()
                if _cuda_usable():
                    return {"kind": "cuda", "detail": name}
                # 有卡但用不了——講清楚為什麼，以及怎麼修
                return {
                    "kind": "cpu",
                    "detail": (f"偵測到 {name}，但缺少 CUDA 函式庫，仍會用 CPU。"
                               "要啟用 GPU 需安裝 CUDA 版 PyTorch"),
                    "gpu_present": name,
                }
        except Exception:
            pass

    if is_apple_silicon():
        # Apple Silicon 上刻意用 CPU：Paraformer 單核就有約 10 倍實時，
        # MPS 帶來的風險大於效益（見 asr/paraformer.py）。
        return {"kind": "cpu", "detail": "Apple Silicon（使用 CPU，速度足夠）"}

    return {"kind": "cpu", "detail": "無 GPU 加速，使用 CPU（較慢但功能相同）"}


def console_utf8() -> None:
    """把 stdout/stderr 強制轉成 UTF-8。

    Windows 主控台預設 cp950(Big5)，遇到簡體字或某些符號會直接拋 UnicodeEncodeError，
    連帶把進度回報打斷。每一個進入點都要呼叫這個。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def summary() -> dict:
    """給 doctor 用的結構化平台摘要。"""
    return {
        "os": os_name(),
        "arch": arch(),
        "apple_silicon": is_apple_silicon(),
        "python": python_version(),
        "python_ok": python_ok(),
        "python_min": ".".join(map(str, MIN_PYTHON)),
        "in_venv": in_venv(),
        "venv_exists": venv_python().exists(),
        "root": str(ROOT),
        "gpu": gpu(),
    }
