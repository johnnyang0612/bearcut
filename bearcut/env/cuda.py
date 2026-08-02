# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""GPU 加速：自動安裝 CUDA 版 PyTorch，並讓 CTranslate2 找得到 CUDA 函式庫。

## 為什麼需要這支

`pip install torch` 在 Windows 預設抓的是 **CPU 版**，不帶任何 CUDA 函式庫。
結果是：機器有 RTX 顯卡、nvidia-smi 跑得動，但辨識全部走 CPU——
一支 1.5 分鐘的影片要跑十分鐘，而使用者完全不知道為什麼。

對「白癡都要能會用」來說，**不能要求使用者自己去查該裝哪個 CUDA 版本**。
所以這裡自動偵測驅動能力、挑對應的 wheel、裝好。

## 兩件事都要做，少一件都不會動

1. **CUDA 版 torch** —— 從 PyTorch 官方 index 裝，不是 PyPI 預設
2. **DLL 搜尋路徑** —— pip 裝的 nvidia 函式庫落在 `site-packages/nvidia/*/bin`，
   Windows 不會自動去那裡找。必須在載入 CTranslate2 之前用
   `os.add_dll_directory()` 加進去，否則照樣是 `cublas64_12.dll is not found`。

只用標準函式庫（bootstrap 會在裝好套件前 import 它）。
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from .platform import VENV, os_name, venv_python

# 驅動支援的 CUDA 版本 → PyTorch wheel index。
# CUDA 有向後相容性：支援 12.9 的驅動可以跑 cu126/cu128 的 build，
# 所以挑「不超過驅動能力的最新版」即可。
_WHEEL_INDEX = [
    (12.8, "cu128"),
    (12.6, "cu126"),
    (11.8, "cu118"),
]


def driver_cuda_version() -> Optional[float]:
    """從 nvidia-smi 讀出驅動支援的最高 CUDA 版本。沒有顯卡回 None。"""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run([smi], capture_output=True, text=True, timeout=15)
        m = re.search(r"CUDA Version:\s*([\d.]+)", out.stdout or "")
        return float(m.group(1)) if m else None
    except Exception:
        return None


def pick_wheel_tag() -> Optional[str]:
    """挑適合這台機器的 PyTorch CUDA wheel 標籤（cu128 / cu126 / cu118）。"""
    if os_name() == "macos":
        return None                     # macOS 沒有 CUDA
    ver = driver_cuda_version()
    if ver is None:
        return None
    for need, tag in _WHEEL_INDEX:
        if ver >= need:
            return tag
    return None                          # 驅動太舊，不強求


def torch_is_cuda() -> bool:
    """目前裝的 torch 是不是 CUDA 版且真的能用。"""
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def install(progress_cb=None, force: bool = False) -> dict:
    """偵測並安裝 CUDA 版 PyTorch。回 `{ok, tag, reason}`。

    這是**選用的加速**，失敗不該讓安裝流程中斷——CPU 一樣能跑，只是慢。
    """
    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    tag = pick_wheel_tag()
    if not tag:
        return {"ok": False, "tag": None,
                "reason": "沒有偵測到可用的 NVIDIA 顯卡，將使用 CPU（功能相同，速度較慢）"}

    if torch_is_cuda() and not force:
        return {"ok": True, "tag": "已安裝", "reason": "CUDA 版 PyTorch 已就緒"}

    py = str(venv_python()) if venv_python().exists() else sys.executable
    say(10, f"偵測到 NVIDIA 顯卡（驅動支援 CUDA {driver_cuda_version()}），"
            f"改裝 GPU 版 PyTorch（{tag}）…")
    say(15, "  這一步會下載約 2-3GB，但辨識速度可快 5-10 倍，值得等")

    # ⚠️ 一定要先移除再裝，不能只用 --upgrade。
    #
    # torch 的 CPU 與 CUDA build 版本號相同，只差 local version 標記
    # （2.13.0+cpu vs 2.13.0+cu128）。pip 認為版本一樣就跳過不換，
    # 結果是 torch 留在 CPU 版、torchaudio 卻換成了 CUDA 版 →
    # 二進位不相容，torchaudio 直接載不起來（libtorchaudio.pyd 載入失敗），
    # Paraformer 整層掛掉。這比單純沒有 GPU 還糟——原本能跑的變成不能跑。
    #
    # 兩個套件必須一起、從同一個 index 重裝，確保版本一致。
    say(20, "  移除現有的 PyTorch（避免 CPU/CUDA 版本錯配）…")
    subprocess.run([py, "-m", "pip", "uninstall", "-y", "torch", "torchaudio"],
                   capture_output=True)

    cmd = [py, "-m", "pip", "install",
           "torch", "torchaudio",
           "--index-url", f"https://download.pytorch.org/whl/{tag}"]
    try:
        r = subprocess.run(cmd, timeout=3600)
    except subprocess.TimeoutExpired:
        _restore_cpu_torch(py)
        return {"ok": False, "tag": tag, "reason": "下載逾時，已還原 CPU 版"}
    if r.returncode != 0:
        # 裝失敗一定要把 CPU 版裝回去——否則使用者會落在「兩個都沒有」的狀態
        _restore_cpu_torch(py)
        return {"ok": False, "tag": tag,
                "reason": "GPU 版 PyTorch 安裝失敗，已還原 CPU 版（功能不受影響，只是較慢）"}

    # CTranslate2（faster-whisper 的後端）需要 cuBLAS 與 cuDNN，
    # 這兩個不在 torch 裡，要另外裝 nvidia 的 pip 套件。
    say(60, "  安裝 CTranslate2 需要的 CUDA 函式庫…")
    subprocess.run([py, "-m", "pip", "install", "--upgrade",
                    "nvidia-cublas-cu12", "nvidia-cudnn-cu12"], check=False)

    say(90, "GPU 加速已啟用")
    return {"ok": True, "tag": tag, "reason": f"已安裝 CUDA 版 PyTorch（{tag}）"}


def _restore_cpu_torch(py: str) -> None:
    """GPU 版裝失敗時把 CPU 版裝回去。

    半裝好的狀態（torch 被移除、CUDA 版又沒裝成）比沒有 GPU 糟得多——
    使用者會從「慢但能用」變成「完全不能用」。
    """
    subprocess.run([py, "-m", "pip", "install", "torch>=2.2.0", "torchaudio>=2.2.0"],
                   capture_output=True)


def _nvidia_dll_dirs() -> List[Path]:
    """pip 安裝的 nvidia 函式庫所在目錄。"""
    dirs = []
    for base in (VENV / "Lib" / "site-packages", Path(sys.prefix) / "Lib" / "site-packages"):
        nv = base / "nvidia"
        if not nv.is_dir():
            continue
        for sub in nv.iterdir():
            for name in ("bin", "lib"):
                d = sub / name
                if d.is_dir():
                    dirs.append(d)
    return dirs


def enable_dll_search() -> int:
    """把 nvidia 函式庫目錄加進 DLL 搜尋路徑。回加了幾個。

    **這一步不做，前面裝的都是白費的。** pip 把 cuBLAS/cuDNN 裝在
    `site-packages/nvidia/*/bin`，Windows 不會去那裡找 DLL。

    ## 為什麼要同時改 PATH，不能只用 add_dll_directory

    `os.add_dll_directory()` 只影響**Python 自己載入擴充模組**時的搜尋路徑。
    但 CTranslate2 是原生模組，它在**第一次推論時**才動態 LoadLibrary 去載 cuBLAS，
    走的是 Windows 預設搜尋順序——那條路徑不吃 add_dll_directory。

    症狀非常有誤導性：`add_dll_directory` 回報成功加了 6 個目錄，
    DLL 也確實在那些目錄裡，模型還「建構成功」，然後第一次 encode 才報
    `cublas64_12.dll is not found`。

    可靠的做法是把目錄前置到 `PATH`——預設搜尋順序一定會看 PATH。
    兩種都做，因為不同版本的 CTranslate2 載入方式不同。

    必須在 import faster_whisper 之前呼叫。
    """
    if os_name() != "windows":
        return 0                         # Linux 靠 RPATH，不需要這一步

    dirs = _nvidia_dll_dirs()
    if not dirs:
        return 0

    paths = [str(d) for d in dirs]
    for p in paths:
        try:
            os.add_dll_directory(p)
        except (OSError, AttributeError):
            pass

    # 前置到 PATH（去重，避免重複呼叫時無限膨脹）
    cur = os.environ.get("PATH", "")
    have = set(cur.split(os.pathsep))
    add = [p for p in paths if p not in have]
    if add:
        os.environ["PATH"] = os.pathsep.join(add) + os.pathsep + cur
    return len(paths)


def status() -> dict:
    """給 doctor 用的摘要。"""
    ver = driver_cuda_version()
    return {
        "driver_cuda": ver,
        "wheel_tag": pick_wheel_tag(),
        "torch_cuda": torch_is_cuda(),
        "dll_dirs": len(_nvidia_dll_dirs()),
    }
