# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""授權碼存放。

## 為什麼需要這一層

`update.py` 的 `check()` / `install_from()` 一直都收 token 參數，但**沒有任何地方
存 token**——客戶拿到授權碼之後無處可貼，只能每次打指令都手動帶 `--token`。
這一層就是那個「貼一次就記住」的地方。

## 存在哪裡，為什麼不存在程式資料夾

授權碼跟著**人**，不跟著某一份解壓縮出來的程式。存在使用者設定目錄有三個好處：

- 使用者重新下載新版 BearCut、把舊資料夾整個刪掉，授權碼還在
- 不會被打包進 ZIP 外流（`package.py` 只收 repo 內的檔案）
- 不會被 git 意外收進版控

| 平台 | 位置 |
|---|---|
| Windows | `%APPDATA%\\BearCut\\auth.json` |
| macOS | `~/Library/Application Support/BearCut/auth.json` |
| Linux | `$XDG_CONFIG_HOME/bearcut/auth.json`（預設 `~/.config`） |

## 取用順序

環境變數 `BEARCUT_TOKEN` **蓋過**設定檔。CI 與進階使用者要能在不動使用者設定的
前提下臨時換一組憑證；反過來讓設定檔蓋過環境變數，會讓「我明明設了環境變數卻沒生效」
變成查不出來的鬼問題。

## 檔案權限

寫完就把權限收成只有自己讀得到。這不是萬無一失的保護（拿得到你帳號的人本來就
拿得到），但能擋掉共用電腦上「另一個帳號順手打開來看」這種最常見的外洩。
收不成功不擋流程——只警告，因為擋下來的代價是客戶連授權碼都存不了。
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

APP_DIR_NAME = "BearCut"
ENV_TOKEN = "BEARCUT_TOKEN"


def config_dir() -> Path:
    """使用者設定目錄（不是程式資料夾）。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_DIR_NAME.lower()


def auth_path() -> Path:
    return config_dir() / "auth.json"


def _lock_down(path: Path) -> Optional[str]:
    """把檔案權限收成只有自己看得到。回錯誤訊息字串，成功回 None。"""
    try:
        if sys.platform.startswith("win"):
            # Windows 的 chmod 只管唯讀旗標，擋不住別的帳號讀取。
            # icacls：切斷繼承（/inheritance:r）再只授權目前使用者完全控制。
            user = os.environ.get("USERNAME") or ""
            if not user:
                return "找不到目前的 Windows 使用者名稱，略過權限設定"
            r = subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode != 0:
                return (r.stderr or r.stdout or "").strip()[:200]
        else:
            os.chmod(path, 0o600)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return None


def _read() -> dict:
    p = auth_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        # 設定檔壞掉不該讓整個程式跑不動——當作沒設定，使用者重貼一次就好
        return {}


def save_token(token: str, feed: Optional[str] = None) -> dict:
    """存授權碼。回 `{ok, path, warning, error}`。"""
    token = (token or "").strip()
    if not token:
        return {"ok": False, "error": "授權碼是空的。請把信裡那一串完整貼上。"}

    d = _read()
    d["token"] = token
    if feed:
        d["feed"] = feed.strip()

    p = auth_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return {"ok": False,
                "error": f"寫不進設定檔（{p}）：{e}\n"
                         "請確認這個資料夾可以寫入，或改用環境變數 BEARCUT_TOKEN。"}

    warn = _lock_down(p)
    return {"ok": True, "path": str(p), "masked": mask(token),
            "feed": d.get("feed"),
            "warning": (f"授權碼已存好，但檔案權限沒收緊（{warn}）。"
                        "如果這台電腦有別人的帳號，請自行確認檔案權限。" if warn else None),
            "error": None}


def load_token() -> Optional[str]:
    """取授權碼。環境變數優先，其次設定檔。都沒有回 None。"""
    env = (os.environ.get(ENV_TOKEN) or "").strip()
    if env:
        return env
    tok = (_read().get("token") or "").strip()
    return tok or None


def load_feed() -> Optional[str]:
    """取存起來的更新來源。環境變數 BEARCUT_UPDATE_FEED 由 update.py 自己處理，
    這裡只回設定檔裡的值（優先序低於環境變數）。"""
    feed = (_read().get("feed") or "").strip()
    return feed or None


def clear_token() -> dict:
    """刪掉設定檔。回 `{ok, path, existed}`。"""
    p = auth_path()
    if not p.exists():
        return {"ok": True, "existed": False, "path": str(p),
                "env": bool((os.environ.get(ENV_TOKEN) or "").strip())}
    try:
        p.unlink()
    except OSError as e:
        return {"ok": False, "existed": True, "path": str(p),
                "error": f"刪不掉設定檔（{p}）：{e}"}
    return {"ok": True, "existed": True, "path": str(p),
            "env": bool((os.environ.get(ENV_TOKEN) or "").strip())}


def mask(token: str) -> str:
    """只露頭尾——log 或畫面上不該出現完整授權碼。"""
    t = (token or "").strip()
    if len(t) <= 8:
        return "*" * len(t)
    return f"{t[:4]}{'*' * 6}{t[-4:]}"


def status() -> dict:
    """目前的授權狀態（給 doctor 與 UI 用）。不回傳完整 token。"""
    env = (os.environ.get(ENV_TOKEN) or "").strip()
    filed = (_read().get("token") or "").strip()
    tok = env or filed
    return {
        "has_token": bool(tok),
        "source": "環境變數 BEARCUT_TOKEN" if env else ("設定檔" if filed else None),
        "masked": mask(tok) if tok else None,
        "path": str(auth_path()),
        "feed": load_feed(),
    }
