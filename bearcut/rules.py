# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""規則包載入。

BearCut 把「剪輯的手感」和「執行的機制」分開：

- **引擎**（這個套件的其餘部分）＝ 怎麼辨識、怎麼下刀、怎麼編碼。穩定、少動。
- **規則包**（`rulepack/`）＝ 門檻多嚴、prompt 怎麼問、字幕長什麼樣。持續迭代。

分開的理由很實際：剪輯手感會一直調（這個門檻鬆一點、那句 prompt 講清楚一點），
而使用者不該為了拿到這些改進就重新下載整個程式。規則包可以單獨更新。

## 讀取順序（後者覆蓋前者）
1. `rulepack/` 內建的基礎規則
2. 使用者的 `settings.json`（局部覆寫，只寫想改的那幾項）
3. 環境變數（給 CI 與臨時測試用）

## 慣例
JSON 裡以 `$` 開頭的鍵一律是註解，讀取時會被略過。
把說明寫在資料旁邊，改門檻的人才看得到「為什麼是這個值」。
"""

import json
import os
from pathlib import Path
from typing import Any, Optional

from .env.platform import ROOT

RULEPACK_DIR = ROOT / "rulepack"
USER_SETTINGS = ROOT / "settings.json"

_cache: Optional["Rules"] = None


class RulepackError(RuntimeError):
    """規則包載入或相容性問題。訊息要讓使用者知道下一步怎麼辦。"""


def _strip_comments(obj: Any) -> Any:
    """遞迴移除 `$` 開頭的註解鍵。"""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("$")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def _deep_merge(base: dict, over: dict) -> dict:
    """把 over 疊到 base 上。巢狀 dict 逐層合併，其餘型別直接取代。

    這樣使用者只要寫想改的那一項，不必抄整份設定。
    """
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _key_ci(d: dict, key: str):
    """在 dict 裡以不分大小寫的方式找鍵，回傳實際的鍵名或 None。"""
    if not isinstance(d, dict):
        return None
    if key in d:
        return key
    low = key.lower()
    for k in d:
        if k.lower() == low:
            return k
    return None


def _ver_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except (ValueError, AttributeError):
        return (0,)


class Rules:
    """載入好的規則。用點路徑取值：`rules.get("silence.noise_db")`。"""

    def __init__(self, meta: dict, values: dict, source: Path):
        self.meta = meta
        self.values = values
        self.source = source

    # --- 取值 ---

    def get(self, path: str, default: Any = None) -> Any:
        """點路徑取值，例：`get("safety.max_cut_ratio")`。"""
        cur: Any = self.values
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def section(self, name: str) -> dict:
        """取整個區塊，例：`section("silence")`。"""
        v = self.get(name, {})
        return dict(v) if isinstance(v, dict) else {}

    # --- prompt ---

    def has_prompt(self, name: str) -> bool:
        """這份判準在不在規則包裡。

        探測 Pro 功能一律用這個，**不要用「渲染一次看會不會炸」**——
        渲染失敗有兩種原因（檔案不存在／變數沒帶），混在一起會讓
        「prompt 多了一個變數」被誤報成「你沒有 Pro」，付了錢的人被叫去買。
        """
        return (RULEPACK_DIR / "prompts" / f"{name}.md").exists()

    def prompt(self, name: str, **vars_) -> str:
        """讀 `rulepack/prompts/<name>.md` 並代入變數。

        用 `str.format` 而非樣板引擎：prompt 是純文字，多拉一個相依不划算。
        prompt 裡若要出現真正的大括號，寫成 `{{` 與 `}}`。
        """
        p = RULEPACK_DIR / "prompts" / f"{name}.md"
        if not p.exists():
            raise RulepackError(
                f"找不到 prompt「{name}」（預期位置 {p}）。"
                "規則包可能不完整，請重新下載或執行 bearcut update。")
        text = p.read_text(encoding="utf-8")
        if not vars_:
            return text
        try:
            return text.format(**vars_)
        except KeyError as e:
            raise RulepackError(
                f"prompt「{name}」需要變數 {e}，但呼叫端沒有提供。") from e

    # --- 資訊 ---

    @property
    def version(self) -> str:
        return self.meta.get("version", "unknown")

    def describe(self) -> str:
        return f"{self.meta.get('title', '規則包')} v{self.version}"


def _check_compat(meta: dict) -> None:
    from . import __version__ as engine_version

    lo, hi = meta.get("engine_min"), meta.get("engine_max")
    ev = _ver_tuple(engine_version)
    if lo and ev < _ver_tuple(lo):
        raise RulepackError(
            f"規則包 v{meta.get('version')} 需要 BearCut {lo} 以上，"
            f"目前是 {engine_version}。請更新 BearCut，或改用較舊的規則包。")
    if hi and ev > _ver_tuple(hi):
        raise RulepackError(
            f"規則包 v{meta.get('version')} 只支援到 BearCut {hi}，"
            f"目前是 {engine_version}。請執行 bearcut update 取得新版規則包。")


DEFAULT_PROFILE = "balanced"


def profiles() -> dict:
    """可選的模式（效率／平衡／精準）。給 UI 產生選項用。

    直接讀原始 JSON，不走 `load()`——因為給人看的名稱與說明是寫成 `$name`、
    `$desc` 的，而 `load()` 會把 `$` 開頭的鍵當註解剝掉。
    """
    th = RULEPACK_DIR / "thresholds.json"
    if not th.exists():
        return {}
    try:
        raw = json.loads(th.read_text(encoding="utf-8")).get("profiles") or {}
    except json.JSONDecodeError:
        return {}

    out = {}
    for key, p in raw.items():
        if key.startswith("$") or not isinstance(p, dict):
            continue
        out[key] = {"name": p.get("$name", key),
                    "desc": p.get("$desc", ""),
                    "tradeoff": p.get("$tradeoff", "")}
    return out


def load(refresh: bool = False, profile: Optional[str] = None) -> Rules:
    """載入規則包（含 profile 與使用者覆寫）。結果會快取。

    `profile` 是「效率／平衡／精準」那一組預設。使用者只需要選快或準，
    不該被問 model_size 或要不要二審——那是工程師的語言。
    """
    global _cache
    if _cache is not None and not refresh and profile is None:
        return _cache

    meta_path = RULEPACK_DIR / "rulepack.json"
    if not meta_path.exists():
        raise RulepackError(
            f"找不到規則包（預期位置 {RULEPACK_DIR}）。\n"
            "若是從 git clone 取得，規則包應該隨附；若缺少請重新下載。")

    try:
        meta = _strip_comments(json.loads(meta_path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as e:
        raise RulepackError(f"規則包描述檔格式錯誤：{e}") from e

    _check_compat(meta)

    values: dict = {}
    th = RULEPACK_DIR / meta.get("files", {}).get("thresholds", "thresholds.json")
    if th.exists():
        try:
            values = _strip_comments(json.loads(th.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            raise RulepackError(f"{th.name} 格式錯誤：{e}") from e

    # profile：整組套用（環境變數 > 參數 > 使用者設定 > 內建預設）
    want = (os.environ.get("BEARCUT_PROFILE") or profile
            or values.get("profile") or DEFAULT_PROFILE)
    prof = (values.get("profiles") or {}).get(want)
    if prof:
        values = _deep_merge(values, {k: v for k, v in prof.items()
                                      if not k.startswith("$")})
        values["_active_profile"] = want
    elif want and want != DEFAULT_PROFILE:
        raise RulepackError(
            f"不認得的模式「{want}」。可用："
            + "、".join(k for k in (values.get("profiles") or {})
                        if not k.startswith("$")))

    # 使用者覆寫：只寫想改的那幾項即可
    if USER_SETTINGS.exists():
        try:
            user = _strip_comments(json.loads(USER_SETTINGS.read_text(encoding="utf-8")))
            values = _deep_merge(values, user)
        except json.JSONDecodeError as e:
            # 使用者設定壞掉不該讓整個程式跑不動——忽略並繼續用內建值比較友善
            print(f"⚠ settings.json 格式錯誤，已略過：{e}")

    # 環境變數覆寫，給 CI 與臨時測試用：BEARCUT_RULE_silence.noise_db=-35
    #
    # ⚠️ 比對必須不分大小寫：Windows 的環境變數不分大小寫，os.environ 會把鍵
    # 全部轉成大寫（BEARCUT_RULE_SILENCE.NOISE_DB），直接拿去對小寫的規則鍵會
    # 對不上，而且是**默默失效**——在 Linux/macOS 測都正常，只有 Windows 壞掉。
    for k, v in os.environ.items():
        if not k.upper().startswith("BEARCUT_RULE_"):
            continue
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            parsed = v

        cur = values
        parts = k[len("BEARCUT_RULE_"):].split(".")
        for p in parts[:-1]:
            match = _key_ci(cur, p)
            if match is None:
                cur = cur.setdefault(p, {})
            else:
                if not isinstance(cur[match], dict):
                    cur[match] = {}
                cur = cur[match]
        cur[_key_ci(cur, parts[-1]) or parts[-1]] = parsed

    _cache = Rules(meta, values, RULEPACK_DIR)
    return _cache


def get(path: str, default: Any = None) -> Any:
    """便利函式：`rules.get("silence.noise_db")`。"""
    return load().get(path, default)
