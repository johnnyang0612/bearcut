# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""ffmpeg / ffprobe 的定位與自動下載。

**這支檔案只准用標準函式庫**（理由同 platform.py）。

設計取捨
--------
1. **一律優先用 vendor/ 裡自己的那份。** 使用者系統上的 ffmpeg 版本、編譯選項、
   有沒有帶到需要的編碼器全都不可控；自己帶一份才能保證兩台機器輸出一致。
2. **不動系統 PATH、不要求管理員權限。** 解壓到專案底下的 vendor/bin 就好——
   「下載 ZIP 解開就能用」的前提是絕不碰系統設定。
3. **找不到就下載，下載失敗給人話。** 錯誤訊息要告訴使用者能怎麼自己解，
   不是丟一個 traceback。

授權說明：ffmpeg 不隨本專案散布，是在使用者要求下、於安裝時下載到使用者自己的機器，
並以獨立行程被呼叫（subprocess），因此其 GPL/LGPL 不影響本專案的 Apache-2.0 授權。
"""

import os
import shutil
import stat
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .platform import VENDOR, arch, os_name

BIN = VENDOR / "bin"

_UA = {"User-Agent": "BearCut-setup/0.1 (+https://Brightstream.com.tw)"}

# 每個平台可用的來源，依序嘗試。每筆是 (說明, [下載網址...])
# 之所以列多個網址：單一鏡像掛掉時還有退路，這是安裝失敗最常見的原因。
SOURCES = {
    ("windows", "x86_64"): [
        ("BtbN static (GitHub)",
         ["https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
          "ffmpeg-master-latest-win64-gpl.zip"]),
        ("gyan.dev essentials",
         ["https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"]),
    ],
    ("windows", "arm64"): [
        ("BtbN static (GitHub)",
         ["https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
          "ffmpeg-master-latest-winarm64-gpl.zip"]),
    ],
    # macOS 的靜態編譯把 ffmpeg 與 ffprobe 拆成兩包，所以這裡是兩個網址
    ("macos", "arm64"): [
        ("martin-riedl arm64",
         ["https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffmpeg.zip",
          "https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffprobe.zip"]),
    ],
    ("macos", "x86_64"): [
        ("martin-riedl amd64",
         ["https://ffmpeg.martin-riedl.de/redirect/latest/macos/amd64/release/ffmpeg.zip",
          "https://ffmpeg.martin-riedl.de/redirect/latest/macos/amd64/release/ffprobe.zip"]),
        ("evermeet.cx",
         ["https://evermeet.cx/ffmpeg/getrelease/zip"]),
    ],
    ("linux", "x86_64"): [
        ("johnvansickle static",
         ["https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"]),
    ],
    ("linux", "arm64"): [
        ("johnvansickle static",
         ["https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz"]),
    ],
}

# macOS 上使用者可能已用 Homebrew 裝過，這些是常見落點
_BREW_DIRS = ["/opt/homebrew/bin", "/usr/local/bin"]


def _exe(name: str) -> str:
    return f"{name}.exe" if os_name() == "windows" else name


def vendored(name: str):
    """vendor/bin 裡那份，存在才回傳。"""
    p = BIN / _exe(name)
    return p if p.exists() else None


def find(name: str = "ffmpeg"):
    """依序找 vendor/ → PATH → Homebrew，回 Path 或 None。"""
    p = vendored(name)
    if p:
        return p
    w = shutil.which(name)
    if w:
        return Path(w)
    if os_name() == "macos":
        for d in _BREW_DIRS:
            c = Path(d) / name
            if c.exists():
                return c
    return None


def ready() -> bool:
    """ffmpeg 與 ffprobe 都到位才算可用——只有其中一個是不能跑的。"""
    return find("ffmpeg") is not None and find("ffprobe") is not None


def version(name: str = "ffmpeg"):
    """回版本字串首行，取不到回 None。"""
    exe = find(name)
    if not exe:
        return None
    import subprocess
    try:
        out = subprocess.run([str(exe), "-version"], capture_output=True,
                             text=True, timeout=15)
        first = (out.stdout or "").splitlines()
        return first[0].strip() if first else None
    except Exception:
        return None


def _download(url: str, dest: Path, progress_cb=None, lo: float = 0.0,
              hi: float = 100.0) -> None:
    """下載並回報進度。檔案上百 MB，沒有進度使用者會以為當掉。

    lo/hi 把這次下載映射到整體進度的某一段，讓多檔下載（macOS 的 ffmpeg + ffprobe）
    合起來仍然單調遞增——進度條倒退會讓人以為出錯了。
    回報有節流：只在整數百分比變動時才發，否則幾百行洗畫面同樣像壞掉。
    """
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        last = -1
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if not progress_cb:
                continue
            frac = (done / total) if total else 0.0
            pct = lo + (hi - lo) * frac
            if int(pct) != last:
                last = int(pct)
                mb = f"{done // 1048576}MB" + (f" / {total // 1048576}MB" if total else "")
                progress_cb(pct, f"下載 ffmpeg… {mb}")


def _harvest(tree: Path) -> int:
    """把解開的目錄樹裡的 ffmpeg/ffprobe 撈出來放進 vendor/bin。

    各家壓縮檔的目錄結構都不一樣（有的在 bin/ 下、有的在版本資料夾下、有的就在根目錄），
    所以不猜路徑，直接走訪整棵樹找檔名。
    """
    BIN.mkdir(parents=True, exist_ok=True)
    wanted = {_exe("ffmpeg"), _exe("ffprobe")}
    got = 0
    for p in tree.rglob("*"):
        if p.is_file() and p.name in wanted:
            dst = BIN / p.name
            shutil.copy2(p, dst)
            if os_name() != "windows":
                dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            got += 1
    return got


def _extract(archive: Path, into: Path) -> None:
    if archive.suffix == ".zip" or zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)
    else:                                   # .tar.xz / .tar.gz
        with tarfile.open(archive) as t:
            t.extractall(into)


def install(progress_cb=None, force: bool = False) -> dict:
    """下載並安裝 ffmpeg 到 vendor/bin。回 {ok, source, ffmpeg, ffprobe, error}。"""
    def report(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not force and ready():
        return {"ok": True, "source": "已存在", "ffmpeg": str(find("ffmpeg")),
                "ffprobe": str(find("ffprobe")), "error": None}

    key = (os_name(), arch())
    candidates = SOURCES.get(key)
    if not candidates:
        return {"ok": False, "source": None, "error":
                f"沒有對應 {key[0]}/{key[1]} 的 ffmpeg 自動下載來源。"
                f"請自行安裝 ffmpeg 並確認 `ffmpeg -version` 可執行。"}

    errors = []
    for label, urls in candidates:
        try:
            report(2, f"取得 ffmpeg（來源：{label}）")
            with tempfile.TemporaryDirectory(prefix="bearcut-ffmpeg-") as td:
                tmp = Path(td)
                # 下載佔整體 2~85%，多個檔案平分這一段，確保進度單調遞增
                span = 83.0 / len(urls)
                for i, url in enumerate(urls):
                    name = url.rstrip("/").split("/")[-1] or f"part{i}"
                    if not name.endswith((".zip", ".xz", ".gz", ".tar")):
                        name += ".zip"
                    pkg = tmp / f"{i}_{name}"
                    _download(url, pkg, progress_cb,
                              lo=2 + span * i, hi=2 + span * (i + 1))
                    _extract(pkg, tmp / f"x{i}")
                report(90, "解壓縮…")
                got = _harvest(tmp)
            if ready():
                report(100, "ffmpeg 就緒")
                return {"ok": True, "source": label, "ffmpeg": str(find("ffmpeg")),
                        "ffprobe": str(find("ffprobe")), "error": None}
            errors.append(f"{label}: 解開了但找不到執行檔（取得 {got} 個）")
        except (urllib.error.URLError, OSError, zipfile.BadZipFile,
                tarfile.TarError) as e:
            errors.append(f"{label}: {e}")

    return {"ok": False, "source": None, "error":
            "ffmpeg 自動下載失敗。你可以自己裝一份（裝好後重跑即可）：\n"
            "  Windows : winget install Gyan.FFmpeg\n"
            "  macOS   : brew install ffmpeg\n"
            "  Linux   : sudo apt install ffmpeg\n"
            "嘗試過的來源：\n  - " + "\n  - ".join(errors)}
