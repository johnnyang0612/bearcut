# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""程式本體更新（`update.py` 更新的是規則包，這支更新的是引擎）。

## 為什麼要有這條

規則包更新解決的是「剪輯手感」，但程式本身有 bug 時——例如啟動器在某些
Windows 上整個跑不起來——使用者唯一的路是自己去重新下載、解壓、蓋回去。
那條路很長，而且他多半是在「已經壞掉」的狀態下被要求走完它。

## 三個絕對不能碰的東西

| 路徑 | 為什麼 |
|---|---|
| `.venv/` | 幾百 MB 的套件，砍掉要重裝 |
| `vendor/` | **好幾 GB 的模型與 FFmpeg**，砍掉客戶要重下一整晚 |
| 使用者的影片與產出 | 那是他的東西 |

前兩者不在 ZIP 裡，所以只要「只複製進去、絕不刪除」，它們天生就安全。
這支模組沒有任何 `rmtree` 打在安裝目錄上——這是刻意的。

## `rulepack/` 的特別處理

引擎 ZIP 裡帶著一份**免費**規則包。直接覆蓋會把 Pro 訂閱者降級回免費版——
那正是 `update.py` 的 `_merge_into` 修掉的同一類錯誤，不能在這裡重犯。

所以規則包一律**疊加**，而且 `rulepack.json` 有額外規則：
裝的是 Pro 包時保留原本的，只更新底包的門檻與 prompt。
結果是「新的底包 + 原本的 Pro 內容 + Pro 的身分」，三者都對。

## 失敗要回得去

覆蓋前先把會被蓋到的檔案備份起來，蓋完做一次健康檢查（關鍵檔在不在、
版本讀不讀得到）。不過就整批還原。半套的安裝比舊版更糟。
"""

import json
import os
import shutil
import tempfile
import urllib.error
import zipfile
from pathlib import Path
from typing import Callable, List, Optional

from . import __version__
from .env.platform import ROOT
from .rules import _ver_tuple
from .update import (DEFAULT_FEED, _asset_sha256, _fetch_json, _merge_into,
                     _sha256)

BACKUP_DIR = ROOT / ".engine_backup"

# 這些路徑更新時完全跳過。不是「先刪再寫」——是根本不碰。
PROTECTED = ("vendor/", ".venv/", "models/", "dist/", ".git/",
             ".rulepack_backup/", ".engine_backup/")

# 少了任何一個，裝完就是壞的
VITAL = ("cli.py", "bootstrap.py", "bearcut/__init__.py", "bearcut/cli.py",
         "START_HERE.bat")


def _is_protected(rel_posix: str) -> bool:
    return any(rel_posix.startswith(p) for p in PROTECTED)


def check(feed: Optional[str] = None) -> dict:
    """看有沒有新版**程式**。回 `{available, current, latest, url, sha256, notes}`。"""
    feed = feed or os.environ.get("BEARCUT_ENGINE_FEED") or DEFAULT_FEED
    try:
        data = _fetch_json(feed)
    except urllib.error.HTTPError as e:
        msg = ("更新來源上還沒有發布任何版本，你目前用的就是最新的。"
               if e.code == 404 else f"更新來源回應 HTTP {e.code}，稍後再試。")
        return {"available": False, "current": __version__, "error": msg}
    except Exception as e:
        return {"available": False, "current": __version__,
                "error": f"連不上更新來源：{e}"}

    # 引擎包叫 BearCut-<版本>.zip；規則包叫 rulepack*.zip，別抓錯
    asset = None
    for a in (data.get("assets") or []):
        name = a.get("name", "")
        if name.lower().startswith("bearcut") and name.endswith(".zip"):
            asset = a
            break
    if not asset:
        return {"available": False, "current": __version__,
                "error": "這個版本沒有附程式包。"}

    latest = (data.get("tag_name") or "").lstrip("v")
    return {
        "available": _ver_tuple(latest) > _ver_tuple(__version__),
        "current": __version__,
        "latest": latest,
        "url": asset.get("browser_download_url"),
        "size": asset.get("size"),
        "sha256": _asset_sha256(asset),
        "notes": (data.get("body") or "")[:800],
    }


def _plan(stage: Path) -> List[tuple]:
    """列出要複製的 (來源, 相對路徑)。跳過受保護路徑。"""
    out = []
    for src in stage.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(stage).as_posix()
        if _is_protected(rel):
            continue
        out.append((src, rel))
    return out


def _health(root: Path) -> Optional[str]:
    """裝完之後這份程式還完整嗎？回問題描述，沒問題回 None。"""
    for v in VITAL:
        if not (root / v).exists():
            return f"缺少 {v}"
    try:
        text = (root / "bearcut" / "__init__.py").read_text(encoding="utf-8")
        if "__version__" not in text:
            return "bearcut/__init__.py 讀不到版本號"
    except OSError as e:
        return f"bearcut/__init__.py 讀不開（{e}）"
    return None


def apply(url: str, expect_sha: Optional[str] = None,
          progress_cb: Optional[Callable] = None) -> dict:
    """下載並套用新版程式。回 `{ok, version, backup, error}`。"""
    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    with tempfile.TemporaryDirectory(prefix="bearcut-engine-") as td:
        tmp = Path(td)
        zpath = tmp / "engine.zip"

        say(10, "下載新版程式…")
        try:
            import urllib.request
            req = urllib.request.Request(
                url, headers={"User-Agent": "BearCut-selfupdate/0.1"})
            with urllib.request.urlopen(req, timeout=300) as r, open(zpath, "wb") as f:
                shutil.copyfileobj(r, f)
        except Exception as e:
            return {"ok": False, "error": f"下載失敗：{e}\n目前的版本沒有被動到，可以照常使用。"}

        got = _sha256(zpath)
        want = (expect_sha or "").strip()
        if want.lower().startswith("sha256:"):
            want = want.split(":", 1)[1]
        if want and got.lower() != want.lower():
            return {"ok": False,
                    "error": "檔案雜湊不符，可能在傳輸中被竄改或損毀，已放棄更新。\n"
                             "目前的版本沒有被動到，可以照常使用。"}

        say(40, "解開…")
        stage = tmp / "stage"
        try:
            with zipfile.ZipFile(zpath) as z:
                z.extractall(stage)
        except zipfile.BadZipFile:
            return {"ok": False, "error": "下載的檔案不是有效的壓縮檔。目前的版本沒有被動到。"}

        # ZIP 解開是一層 BearCut/
        root = stage
        if not (root / "cli.py").exists():
            subs = [p for p in stage.iterdir() if p.is_dir()]
            if len(subs) == 1:
                root = subs[0]
        if not (root / "cli.py").exists():
            return {"ok": False, "error": "下載的程式包結構不對，已放棄更新。"}

        items = _plan(root)
        if not items:
            return {"ok": False, "error": "程式包是空的，已放棄更新。"}

        # 先驗**來源包**完整，不是只驗結果。
        # 因為我們只複製不刪除，一個殘缺的包裝上去之後，舊檔案還在，
        # 結果看起來是完整的——但那是新舊混血：新的 cli.py 配舊的 bootstrap.py。
        # 那種安裝比明確失敗更難查。
        missing = [v for v in VITAL if not (root / v).exists()]
        if missing:
            return {"ok": False,
                    "error": f"下載的程式包不完整（缺少 {'、'.join(missing)}），已放棄更新。\n"
                             "目前的版本沒有被動到，可以照常使用。\n"
                             "多半是下載中斷，請再試一次。"}

        # 裝的是 Pro 包時，保留它的身分——ZIP 裡那份是免費包的 rulepack.json，
        # 蓋上去等於把訂閱者降級。底包的門檻與 prompt 照樣更新。
        keep_pack_identity = False
        cur_meta = ROOT / "rulepack" / "rulepack.json"
        if cur_meta.exists():
            try:
                name = json.loads(cur_meta.read_text(encoding="utf-8")).get("name", "")
                keep_pack_identity = bool(name) and name != "bearcut-base"
            except (json.JSONDecodeError, OSError):
                pass

        say(60, "備份現有版本…")
        if BACKUP_DIR.exists():
            shutil.rmtree(BACKUP_DIR, ignore_errors=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backed = []
        for _, rel in items:
            cur = ROOT / rel
            if cur.exists():
                dst = BACKUP_DIR / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cur, dst)
                backed.append(rel)

        say(80, "套用…")
        try:
            for src, rel in items:
                if keep_pack_identity and rel == "rulepack/rulepack.json":
                    continue
                dst = ROOT / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        except OSError as e:
            _restore(backed)
            return {"ok": False,
                    "error": f"套用失敗（{e}），已還原成原本的版本。\n"
                             "常見原因是 BearCut 還開著，請先關掉再試一次。"}

        broken = _health(ROOT)
        if broken:
            say(95, "更新後檢查沒過，還原…")
            _restore(backed)
            return {"ok": False,
                    "error": f"更新後檢查沒過（{broken}），已還原成原本的版本。\n"
                             "請把這個訊息回報給我們。"}

    new_ver = _read_version() or "未知"
    say(100, f"已更新到 {new_ver}")
    return {"ok": True, "version": new_ver, "previous": __version__,
            "backup": str(BACKUP_DIR), "files": len(items), "error": None}


def _restore(rels: List[str]) -> None:
    for rel in rels:
        src = BACKUP_DIR / rel
        if src.exists():
            dst = ROOT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _read_version() -> Optional[str]:
    """從磁碟讀版本，不是記憶體裡的 __version__——那還是舊的。"""
    try:
        for line in (ROOT / "bearcut" / "__init__.py").read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def rollback(progress_cb: Optional[Callable] = None) -> dict:
    """還原上一次更新前的版本。"""
    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not BACKUP_DIR.exists():
        return {"ok": False, "error": "沒有備份可以還原。"}
    rels = [p.relative_to(BACKUP_DIR).as_posix()
            for p in BACKUP_DIR.rglob("*") if p.is_file()]
    if not rels:
        return {"ok": False, "error": "備份是空的。"}
    say(50, f"還原 {len(rels)} 個檔案…")
    _restore(rels)
    say(100, "已還原")
    return {"ok": True, "restored": len(rels), "version": _read_version(), "error": None}
