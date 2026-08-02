# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""規則包更新通道。

## 為什麼這件事重要

剪輯的「手感」會一直迭代：這個門檻鬆一點、那句 prompt 講清楚一點、
平台演算法變了所以字卡規則要調。使用者**不該為了拿到這些改進就重新下載整個程式**。

規則包可以單獨更新，引擎不動。這也是免費轉付費的接口：
基礎包隨程式附送，進階包驗證授權後下發。

## 兩條通道

- **GitHub Releases**：免費包。零基礎設施、透明、任何人都能看到內容。
- **自有伺服器**：進階包。需要授權碼，可以按客戶下發不同內容。

## 安全設計

**雜湊必驗。** 規則包會影響剪輯行為，被中間人換掉就等於被控制了剪輯結果。
每個包都附 SHA-256，下載後比對，對不上就整包丟掉。

**一定要能回滾。** 新規則可能不合某人的素材。更新前備份現有的，
`--rollback` 一秒還原——沒有退路的更新機制沒人敢用。

**相容性先擋。** 規則包宣告它需要的引擎版本範圍，不合就明確拒絕並說明，
而不是讓它跑出莫名其妙的結果。
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from . import auth
from .env.platform import ROOT
from .rules import RULEPACK_DIR, RulepackError, _ver_tuple

BACKUP_DIR = ROOT / ".rulepack_backup"
DEFAULT_FEED = "https://api.github.com/repos/johnnyang0612/bearcut/releases/latest"

# 有授權碼的人要去的地方。這不是秘密——它是個要驗授權碼才給東西的公開端點，
# 寫在這裡是為了讓客戶**不必手動貼網址**：他買的是規則包，不是一份設定教學。
PRO_FEED = "https://www.brightstream.com.tw/api/bearcut/feed"

_UA = {"User-Agent": "BearCut-update/0.1", "Accept": "application/vnd.github+json"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _asset_sha256(asset: dict) -> Optional[str]:
    """從 feed 的 asset 取出 SHA-256。取不到回 None（呼叫端要警告，不是靜默放行）。

    GitHub Releases 的 asset 現在帶 `digest`，格式是 `"sha256:<hex>"`；
    自架的 Pro feed 照同一個形狀給。另外容許直接給 `sha256` 欄位——
    自架 feed 拼錯欄位名的代價是整包退回沒有雜湊可驗，不值得為了形式一致而賭。

    拿到的字串一律驗過是不是 64 位十六進位才回傳：feed 給了個空字串或
    `"sha256:"` 這種半截值時，要走「沒給」的警告路徑，不能拿去比對後
    報「雜湊不符」——那會把「來源沒附雜湊」誤報成「檔案被竄改」。
    """
    raw = (asset.get("digest") or "").strip()
    if raw.lower().startswith("sha256:"):
        raw = raw.split(":", 1)[1]
    elif raw:
        return None          # 有 digest 但不是 sha256（換演算法了）→ 當作沒有
    else:
        raw = str(asset.get("sha256") or "").strip()

    raw = raw.lower()
    if len(raw) == 64 and all(c in "0123456789abcdef" for c in raw):
        return raw
    return None


def _pack_hint(asset: dict, data: dict) -> Optional[str]:
    """這個 feed 給的是哪一個包（不是哪一版）。

    feed 若直接宣告 `pack` 就用它；沒有的話從資產檔名推：
    `rulepack-pro-0.1.0.zip` → `bearcut-pro`、`rulepack-0.1.0.zip` → `bearcut-base`。

    檔名推斷是權宜之計——`check()` 不下載就看不到包裡的 `name`，
    而「要不要下載」正是它要回答的問題。推錯的後果只是多問一次要不要更新，
    比「訂閱者永遠收不到包」輕得多。
    """
    declared = (data.get("pack") or asset.get("pack") or "").strip()
    if declared:
        return declared

    name = (asset.get("name") or "").strip()
    if not (name.startswith("rulepack") and name.endswith(".zip")):
        return None
    stem = name[:-4][len("rulepack"):].lstrip("-")
    parts = [p for p in stem.split("-") if p]
    if parts and re.match(r"^\d", parts[-1]):      # 去掉尾巴的版號
        parts = parts[:-1]
    suffix = "-".join(parts)
    return f"bearcut-{suffix}" if suffix else "bearcut-base"


def current() -> dict:
    """目前安裝的規則包資訊。"""
    p = RULEPACK_DIR / "rulepack.json"
    if not p.exists():
        return {"name": None, "version": None}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {k: v for k, v in d.items() if not k.startswith("$")}
    except json.JSONDecodeError:
        return {"name": None, "version": None}


def _fetch_json(url: str, token: Optional[str] = None, timeout: int = 30) -> dict:
    h = dict(_UA)
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def resolve_feed(feed: Optional[str] = None,
                 token: Optional[str] = None) -> str:
    """決定要問哪個更新來源。

    順序：指令參數 > 環境變數 > 登入時存下來的 > **有授權碼就走 Pro** > GitHub。

    「有授權碼就走 Pro」這一段是必要的：客戶付完錢收到的信教他打
    `bearcut login <授權碼>` 然後 `bearcut update`，如果這裡沒有這一層，
    update 會去 GitHub 抓**免費包**——他付了錢、照指示做、什麼都沒拿到，
    而且畫面還會顯示「已是最新版」，完全看不出哪裡不對。

    不叫客戶自己貼 feed 網址，是因為他買的是規則包，不是一份設定教學。
    自架 feed 的人仍可用 --feed 或環境變數蓋過去。
    """
    explicit = (feed or os.environ.get("BEARCUT_UPDATE_FEED")
                or auth.load_feed())
    if explicit:
        return explicit
    return PRO_FEED if (token or auth.load_token()) else DEFAULT_FEED


def check(feed: Optional[str] = None, token: Optional[str] = None) -> dict:
    """看有沒有新版。回 `{available, current, latest, url, sha256, notes}`。

    `token` 不給就自動用 `bearcut login` 存下來的那組（或環境變數）。
    """
    cur = current()
    token = token or auth.load_token()
    feed = resolve_feed(feed, token)
    try:
        data = _fetch_json(feed, token)
    except urllib.error.HTTPError as e:
        # 每一種狀態碼要給不同的下一步。全部回「稍後再試」是錯的指引：
        # 授權碼打錯或訂閱到期，等再久都不會自己好，客戶會一直重按然後來問。
        if e.code == 404:
            # 「這個來源還沒發布任何版本」，不是故障、更不是使用者的網路有問題。
            # 丟原始的 "HTTP Error 404" 給他，他會去重開路由器。
            msg = ("更新來源上還沒有發布任何規則包，你目前用的是隨程式附的版本。"
                   "這不影響剪片。")
        elif e.code == 401:
            msg = ("授權碼無效或沒有帶上。請確認訂閱信裡的授權碼有完整複製，"
                   "再執行一次 bearcut login <授權碼>。\n"
                   "沒有訂閱也可以照常剪片，用的是隨程式附的規則包。")
        elif e.code == 403:
            msg = ("這組授權碼已經到期或被停用，所以拿不到新的規則包。\n"
                   "續訂之後就會恢復。你目前這份規則包仍然可以繼續用，剪片不受影響。")
        elif e.code == 503:
            msg = "更新來源說這個月的規則包還沒發布，稍後再試。這不影響剪片。"
        elif 500 <= e.code < 600:
            msg = f"更新來源伺服器出問題（HTTP {e.code}），稍後再試。這不影響剪片。"
        else:
            msg = f"更新來源回應 HTTP {e.code}，稍後再試。"
        return {"available": False, "current": cur.get("version"),
                "latest": None, "error": msg}
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        # 離線是常態，不是錯誤——安靜地回報「查不到」就好
        return {"available": False, "current": cur.get("version"),
                "latest": None, "error": f"連不上更新來源：{e}"}

    asset = None
    for a in (data.get("assets") or []):
        if a.get("name", "").startswith("rulepack") and a["name"].endswith(".zip"):
            asset = a
            break
    if not asset:
        return {"available": False, "current": cur.get("version"),
                "latest": data.get("tag_name"), "error": "這個版本沒有附規則包"}

    latest = (data.get("tag_name") or "").lstrip("v")
    newer = _ver_tuple(latest) > _ver_tuple(cur.get("version") or "0")

    # 版號一樣不代表沒東西可拿：進階包與免費底包是**不同的包**，
    # 兩邊都叫 0.1.0 是常態。只比版號的話，訂閱者按更新會被告知
    # 「已是最新版」，永遠拿不到他付錢買的東西。
    hint = _pack_hint(asset, data)
    installed = (cur.get("name") or "").strip()
    different = bool(hint and installed and hint != installed)

    return {
        "available": newer or different,
        "current": cur.get("version"),
        "current_pack": installed or None,
        "latest": latest,
        "pack": hint,
        "url": asset.get("browser_download_url"),
        "size": asset.get("size"),
        "notes": (data.get("body") or "")[:800],
        "sha256": _asset_sha256(asset),
    }


def _backup() -> Optional[Path]:
    """備份現有規則包。沒有退路的更新機制沒人敢用。"""
    if not RULEPACK_DIR.exists():
        return None
    cur = current().get("version") or "unknown"
    dst = BACKUP_DIR / f"rulepack-{cur}"
    BACKUP_DIR.mkdir(exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(RULEPACK_DIR, dst)
    return dst


def _verify_pack(d: Path) -> dict:
    """檢查解開的包是不是合法的規則包，並擋掉不相容的版本。"""
    meta_p = d / "rulepack.json"
    if not meta_p.exists():
        raise RulepackError("下載的檔案裡沒有 rulepack.json，不是有效的規則包。")
    meta = {k: v for k, v in json.loads(meta_p.read_text(encoding="utf-8")).items()
            if not k.startswith("$")}

    from . import __version__
    lo, hi = meta.get("engine_min"), meta.get("engine_max")
    ev = _ver_tuple(__version__)
    if lo and ev < _ver_tuple(lo):
        raise RulepackError(
            f"這個規則包需要 BearCut {lo} 以上，你目前是 {__version__}。\n"
            "請先更新 BearCut 本體，或改用較舊的規則包。")
    if hi and ev > _ver_tuple(hi):
        raise RulepackError(
            f"這個規則包只支援到 BearCut {hi}，你目前是 {__version__}。\n"
            "請取得較新的規則包。")
    return meta


def _merge_into(src: Path, dst: Path) -> None:
    """把 src 疊到 dst 上：同名檔覆蓋，dst 原有而 src 沒有的檔案**留著**。

    ## 為什麼不能整包取代

    規則包分層：免費底包帶 `thresholds.json` 與 `prompts/`（引擎的地基），
    進階包只帶自己新增的內容（hook 型錄、字卡特效、校字判準）。
    以前這裡是 `rmtree` 之後 `copytree`——進階包一裝上去就把地基刪了，
    校字、判重複、大字卡全部拋 RulepackError，三種模式從 UI 消失。
    **客戶付了錢，換來一個壞掉的 BearCut。**

    疊加之後底包留著，進階包蓋掉它要蓋的部分，兩層都在。

    代價：若某一版底包想「移除」某個檔案，疊加會讓舊檔留下來。
    那是一個沒人讀的檔案，跟「把地基刪掉」比起來是可以接受的取捨。
    """
    dst.mkdir(parents=True, exist_ok=True)
    for s in src.rglob("*"):
        rel = s.relative_to(src)
        d = dst / rel
        if s.is_dir():
            d.mkdir(parents=True, exist_ok=True)
        else:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)


def _install_health(d: Path) -> Optional[str]:
    """裝完之後引擎還跑得動嗎？回問題描述，沒問題回 None。

    只檢查「少了就不能剪片」的東西。這一層存在的理由是 A-7 的教訓：
    當時測到「檔案有複製過去、rulepack.json 的版號有變」就當作通過，
    但那個包其實會讓引擎整個不能用——**驗證要驗結果可用，不是驗動作完成**。
    """
    if not (d / "rulepack.json").exists():
        return "缺 rulepack.json"
    if not (d / "thresholds.json").exists():
        return "缺 thresholds.json，所有偵測門檻會失效"
    prompts = d / "prompts"
    if not prompts.is_dir() or not any(prompts.glob("*.md")):
        return "缺 prompts/，校字與判斷會整層失效"
    try:
        json.loads((d / "thresholds.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return f"thresholds.json 讀不開（{e}）"
    return None


def install_from(url: str, expect_sha: Optional[str] = None,
                 token: Optional[str] = None,
                 progress_cb: Optional[Callable] = None) -> dict:
    """下載並安裝規則包。回 `{ok, version, backup, error}`。

    `token` 不給就自動用 `bearcut login` 存下來的那組（或環境變數）。
    """
    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    token = token or auth.load_token()

    with tempfile.TemporaryDirectory(prefix="bearcut-pack-") as td:
        tmp = Path(td)
        zpath = tmp / "pack.zip"

        say(10, "下載規則包…")
        h = dict(_UA)
        if token:
            h["Authorization"] = f"Bearer {token}"
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=120) as r, open(zpath, "wb") as f:
                shutil.copyfileobj(r, f)
        except (urllib.error.URLError, OSError) as e:
            return {"ok": False, "error": f"下載失敗：{e}"}

        # 雜湊必驗：規則包會影響剪輯行為，被換掉等於被控制了結果
        got = _sha256(zpath)
        want = (expect_sha or "").strip()
        if want.lower().startswith("sha256:"):     # 有人會把整個 digest 原封傳進來
            want = want.split(":", 1)[1]
        warning = None
        if want:
            if got.lower() != want.lower():
                return {"ok": False,
                        "error": "檔案雜湊不符，可能在傳輸中被竄改或損毀，已放棄安裝。\n"
                                 "原本的規則包沒有被動過，可以照常剪片。"
                                 "請確認更新來源，或稍後再試一次。"}
        else:
            # 免費包走 GitHub 一定有 digest；會走到這裡的是自架 feed 漏給。
            # 擋下來只會讓 Pro 客戶裝不了包，所以照裝——但要講出來，
            # 不能讓「沒驗」跟「驗過了」在畫面上長得一模一樣。
            warning = ("這個更新來源沒有附 SHA-256，無法確認檔案在傳輸中有沒有被動過。\n"
                       "  已照常安裝。如果這不是你自己架的更新來源，建議先確認來源可信。")
            say(45, "來源未附雜湊，略過驗證")

        say(50, "解開並檢查…")
        stage = tmp / "stage"
        try:
            with zipfile.ZipFile(zpath) as z:
                z.extractall(stage)
        except zipfile.BadZipFile:
            return {"ok": False, "error": "下載的檔案不是有效的壓縮檔。"}

        # 有些包會多一層資料夾
        root = stage
        if not (root / "rulepack.json").exists():
            subs = [p for p in stage.iterdir() if p.is_dir()]
            if len(subs) == 1:
                root = subs[0]

        try:
            meta = _verify_pack(root)
        except (RulepackError, json.JSONDecodeError) as e:
            return {"ok": False, "error": str(e)}

        say(70, "備份現有規則包…")
        backup = _backup()

        say(85, "安裝…")
        _merge_into(root, RULEPACK_DIR)

        # 裝完要確認引擎還跑得動，不是確認檔案有複製過去。
        # 進階包只帶自己新增的東西（hook 型錄、字卡特效），不帶 thresholds 與 prompts；
        # 疊加安裝已經保住了底包，但萬一包本身有問題，寧可退回去也不能讓客戶
        # 拿到一個裝好卻不能剪片的 BearCut。
        broken = _install_health(RULEPACK_DIR)
        if broken:
            say(95, "安裝後檢查沒過，還原…")
            shutil.rmtree(RULEPACK_DIR, ignore_errors=True)
            if backup:
                shutil.copytree(backup, RULEPACK_DIR)
                tail = "已經自動還原成原本的規則包，剪片不受影響。"
            else:
                # backup 是 None 只有一種情況：原本就沒有規則包目錄。
                # 那就把剛裝的半套清掉，回到「原本沒有」——留著半套比沒有更糟。
                tail = "已經清掉沒裝完的部分。請重新安裝 BearCut 取得完整的規則包。"
            return {"ok": False,
                    "error": f"這個規則包裝上去之後 BearCut 會不能用（{broken}）。\n"
                             f"{tail}\n"
                             "請把這個訊息回報給規則包的來源。"}

    say(100, f"已更新到規則包 {meta.get('version')}")
    return {"ok": True, "version": meta.get("version"),
            "name": meta.get("name"),
            "backup": str(backup) if backup else None,
            "sha256": got, "verified": bool(want), "warning": warning,
            "error": None}


def rollback(progress_cb: Optional[Callable] = None) -> dict:
    """還原上一版規則包。"""
    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not BACKUP_DIR.exists():
        return {"ok": False, "error": "沒有備份可以還原。"}
    backups = sorted([p for p in BACKUP_DIR.iterdir() if p.is_dir()],
                     key=lambda p: p.stat().st_mtime, reverse=True)
    if not backups:
        return {"ok": False, "error": "沒有備份可以還原。"}

    src = backups[0]
    say(50, f"還原 {src.name}…")
    if RULEPACK_DIR.exists():
        shutil.rmtree(RULEPACK_DIR, ignore_errors=True)
    shutil.copytree(src, RULEPACK_DIR)
    say(100, "已還原")
    return {"ok": True, "restored": src.name, "error": None}
