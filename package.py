#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""打包成可散布的 ZIP。

    python package.py            # 產出 dist/BearCut-<版本>.zip（完整程式）
    python package.py --rulepack # 產出 dist/rulepack-<版本>.zip（只有規則包）

## 刻意不打包的東西

`vendor/`（FFmpeg 與模型，數 GB）與 `.venv/` 都**不進 ZIP**。
使用者第一次雙擊 START_HERE 時會自動下載對應自己平台的版本——
把 Windows 的 FFmpeg 打包給 Mac 使用者沒有意義，而且會讓 ZIP 從 2MB 變成 3GB。

這也是為什麼 bootstrap 要做得夠好：**它是散布策略的一部分，不只是安裝腳本。**
"""

import io
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bearcut import __version__                                    # noqa: E402


def _check_version_sync() -> None:
    """版本號寫在兩個地方，不同步就停下來。

    實際發生過：pyproject.toml 停在 0.1.0、bearcut/__init__.py 已經 0.2.0，
    沒有人發現。版本號對不上會讓 `bearcut upgrade` 判斷錯誤，
    使用者以為自己是最新版。
    """
    import re
    p = Path(__file__).parent / "pyproject.toml"
    m = re.search(r'^version = "([\d.]+)"', p.read_text(encoding="utf-8"),
                  re.M) if p.exists() else None
    if m and m.group(1) != __version__:
        raise SystemExit(
            f"  版本號不同步：pyproject.toml 是 {m.group(1)}，"
            f"bearcut/__init__.py 是 {__version__}。\n"
            f"  兩邊改成一樣再打包。")
from bearcut.env.platform import ROOT, console_utf8                # noqa: E402

console_utf8()

# 不進 ZIP 的目錄與副檔名
SKIP_DIRS = {".git", ".venv", "vendor", "__pycache__", "dist", ".pytest_cache",
             ".mypy_cache", ".ruff_cache", ".idea", ".vscode", "models"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".mp4", ".mov", ".mkv", ".avi", ".mp3", ".wav"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini", "settings.json"}

# 內部文件不進 ZIP。
#
# 這個 repo 是公開的，所以內部文件根本不放進來——真正的防線是「不在 repo 裡」，
# 不是「打包時記得跳過」。下面這組是留著的安全網：萬一有人日後把內部文件
# 放回 docs/，至少不會跟著出貨。
#
# 名單保持這個形狀（相對路徑、正斜線），collect() 會照字面比對。
SKIP_INTERNAL = {"docs/交接.md", "docs/PLAN.md"}


# 打包時強制正規化換行。
#
# 為什麼不能只靠 .gitattributes：那只規範 git 的儲存與簽出，而打包是從**工作目錄**
# 讀檔的。開發機上檔案是什麼換行，ZIP 裡就是什麼換行。
#
# 客戶拿到的是 ZIP，所以 ZIP 必須自己保證正確：
#   .bat / .cmd  → CRLF，否則舊版 cmd.exe 解析多行結構會出錯
#   .command/.sh → LF，否則 macOS 會報 bad interpreter: /bin/bash^M 而完全無法執行
CRLF_EXT = {".bat", ".cmd"}
LF_EXT = {".command", ".sh"}

# assets/messages/*.txt 是 START_HERE.bat 用 type 吐出來的中文訊息。
# 它們在 Windows 主控台顯示，所以要 CRLF——但副檔名是 .txt，不能靠副檔名分辨
# （repo 裡其他 .txt 例如字型授權不該被動）。git 會把它們存成 LF，
# 所以跟 .bat 一樣：ZIP 必須自己保證，不能靠 .gitattributes 或工作目錄的狀態。
CRLF_DIRS = ("assets/messages/",)


def needs_crlf(rel_posix: str, suffix: str) -> bool:
    return suffix in CRLF_EXT or rel_posix.startswith(CRLF_DIRS)


def normalize(data: bytes, suffix: str, rel_posix: str = "") -> bytes:
    want_crlf = needs_crlf(rel_posix, suffix)
    if not want_crlf and suffix not in LF_EXT:
        return data
    # 用 bytes([13]) 而不是跳脫序列：這段常被各種工具轉手，
    # 字面上的反斜線很容易在傳遞途中被吃掉，換算成碼點就不會。
    CR, LF, CRLF = bytes([13]), bytes([10]), bytes([13, 10])
    lf = data.replace(CRLF, LF).replace(CR, LF)
    return lf.replace(LF, CRLF) if want_crlf else lf


# 固定時間戳，讓打包**可重現**：同樣的檔案內容永遠產出同一個 SHA-256。
#
# 為什麼要在意：發版流程是「打包 → 算 SHA → 寫進 release notes → 上傳」。
# 若 ZIP 內嵌打包當下的時間，同樣的內容每打一次雜湊就變一次，於是
#   · SHA 不再能回答「內容有沒有變」——它只回答「有沒有重打包」
#   · 任何一次重打包都會讓已公布的雜湊作廢，即使一個字都沒改
# 這正是雜湊要防的事情的反面。1980-01-01 是 ZIP 格式的紀元起點，
# 也是可重現建置的慣用值。
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


#: 解壓後要可以直接執行的檔案。ZIP 有保存 Unix 權限的欄位，但預設不會寫，
#: 所以 macOS 使用者解壓出來的 .command 沒有 x 權限——**點兩下不會執行**，
#: 只會用文字編輯器打開。客戶的原話：「.bat mac 打不開」。
EXEC_EXT = {".command", ".sh"}


def entry(name: str) -> zipfile.ZipInfo:
    """建一個時間戳與權限都固定的 ZIP 條目。"""
    zi = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
    zi.compress_type = zipfile.ZIP_DEFLATED
    exec_ = Path(name).suffix.lower() in EXEC_EXT
    zi.external_attr = (0o755 if exec_ else 0o644) << 16
    # macOS 的 unzip 只在「建立系統」標成 Unix 時才理會權限位元
    zi.create_system = 3
    return zi


def collect(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_FILES or Path(fn).suffix.lower() in SKIP_EXT:
                continue
            p = Path(dirpath) / fn
            rel = p.relative_to(root)
            if str(rel).replace("\\", "/") in SKIP_INTERNAL:
                continue
            yield p, rel


def pack_rulepack() -> int:
    """只打包 rulepack/，給 GitHub Release 當更新資產。

    檔名必須以 `rulepack` 開頭、`.zip` 結尾——`bearcut/update.py` 的 check() 就是
    照這個規則在 release assets 裡找包的。改名字會讓所有已安裝的使用者收不到更新。

    rulepack.json 放在 ZIP 的**最上層**（update.py 雖然容許多包一層資料夾，
    但少一層就少一個出錯的機會）。
    """
    import hashlib
    import json as _json

    src = ROOT / "rulepack"
    meta_p = src / "rulepack.json"
    if not meta_p.exists():
        print("  ✗ 找不到 rulepack/rulepack.json")
        return 1
    meta = {k: v for k, v in _json.loads(meta_p.read_text(encoding="utf-8")).items()
            if not k.startswith("$")}
    ver = meta.get("version") or "0.0.0"

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"rulepack-{ver}.zip"

    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn in SKIP_FILES or Path(fn).suffix.lower() in SKIP_EXT:
                    continue
                p = Path(dirpath) / fn
                z.writestr(entry(str(p.relative_to(src)).replace("\\", "/")), p.read_bytes())
                n += 1

    # 雜湊要跟著 release notes 一起發：使用者端會拿它比對，對不上就整包丟掉
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"\n  規則包打包完成：{out}")
    print(f"  {n} 個檔案，{out.stat().st_size / 1024:.0f} KB")
    print(f"  版本 {ver}（相容引擎 {meta.get('engine_min')} ~ {meta.get('engine_max') or '不限'}）")
    print(f"  SHA-256  {sha}")
    print("\n  把這個檔案掛上 GitHub Release，並把上面的雜湊寫進 release notes。\n")
    return 0


def main() -> int:
    _check_version_sync()
    if "--rulepack" in sys.argv[1:]:
        return pack_rulepack()

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"BearCut-{__version__}.zip"

    n, total = 0, 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for src, rel in collect(ROOT):
            data = src.read_bytes()
            total += len(data)
            data = normalize(data, src.suffix.lower(),
                             str(rel).replace("\\", "/"))
            # 解壓後是一個 BearCut/ 資料夾，不會把檔案灑滿使用者的下載目錄
            z.writestr(entry(str(Path("BearCut") / rel).replace("\\", "/")), data)
            n += 1

    size_mb = out.stat().st_size / 1048576
    print(f"\n  打包完成：{out}")
    print(f"  {n} 個檔案，原始 {total / 1048576:.1f}MB → 壓縮後 {size_mb:.1f}MB")
    print("\n  使用者拿到後：解壓 → 雙擊 START_HERE → 自動裝好一切。")
    print("  FFmpeg 與模型不在 ZIP 裡，安裝時依平台自動下載。\n")

    # 檢查幾個一定要在的檔案，避免打包漏掉關鍵檔
    must = ["BearCut/README.md", "BearCut/LICENSE", "BearCut/NOTICE",
            "BearCut/TRADEMARK.md", "BearCut/AGENTS.md", "BearCut/CLAUDE.md",
            "BearCut/bootstrap.py", "BearCut/cli.py",
            "BearCut/START_HERE.bat", "BearCut/START_HERE.command",
            "BearCut/rulepack/rulepack.json",
            # 授權檔漏掉就是散布沒有授權聲明的內容，比少一個功能嚴重
            "BearCut/rulepack/LICENSE-RULEPACK.md",
            "BearCut/bearcut/ui/static/index.html"]
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
    missing = [m for m in must if m not in names]
    if missing:
        print("  ⚠ 少了這些必要檔案：")
        for m in missing:
            print(f"      {m}")
        return 1
    print("  必要檔案檢查：全部到齊 ✓\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
