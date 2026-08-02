# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""規則包指紋 —— 外流時可究責。

## 這一層防的是什麼，不防什麼

**不防**：專業破解。任何落到使用者機器上的東西都能被取出，這是硬事實，
本地端 DRM 從來沒有守住過任何東西。

**防的是**：隨手分享。以及外流之後「查不出是誰」。

## 作法：不可見的個別差異

每個授權的規則包帶一組**微小到不影響行為、但足以識別**的差異——
門檻數值的小數末位、陣列的排列順序。外流到公開場合時，比對就知道是哪一份。

這比「加密」實在得多：加密只是把門鎖上（十分鐘可解），
指紋則是**讓洩漏者知道自己會被查出來**，嚇阻效果反而更強。

## 為什麼公開這個機制

Kerckhoffs 原則：安全性不該建立在「對方不知道機制」上。
把作法寫清楚，該遵守的人會遵守；而想外流的人本來就會逆向工程。
說清楚反而讓嚇阻力生效——藏起來的威嚇不會嚇到任何人。
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

MARKER_KEY = "_fp"          # 明碼標記：告訴使用者這份包是誰的，不藏
EPSILON = 1e-6              # 數值擾動幅度：小到不可能改變任何判斷結果


def _derive(licensee: str, salt: str = "") -> Tuple[int, str]:
    """從授權對象推出一組穩定的擾動種子。"""
    h = hashlib.sha256(f"{licensee}|{salt}".encode("utf-8")).hexdigest()
    return int(h[:8], 16), h[:16]


def _perturb(obj: Any, seed: int, depth: int = 0) -> Any:
    """遞迴地在浮點數末位加上極小的擾動。

    只動浮點數：整數（刀數上限）與字串（prompt）動了會改變行為或可讀性。
    擾動幅度 1e-6，比任何門檻的有效位數都小好幾個數量級。
    """
    if isinstance(obj, dict):
        return {k: (v if k.startswith("$") else _perturb(v, seed, depth + 1))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_perturb(v, seed, depth + 1) for v in obj]
    if isinstance(obj, float):
        # 依位置決定加或減，讓每份包的模式不同
        step = ((seed >> (depth % 24)) & 0xFF) - 128
        return round(obj + step * EPSILON, 12)
    return obj


def stamp(pack_dir: str, licensee: str, salt: str = "",
          note: str = "") -> Dict[str, Any]:
    """把指紋蓋進一份規則包（就地修改）。

    回 `{fingerprint, licensee, files}`。
    """
    d = Path(pack_dir)
    seed, fp = _derive(licensee, salt)
    touched = []

    for p in sorted(d.rglob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        data = _perturb(data, seed)
        if p.name == "rulepack.json" and isinstance(data, dict):
            # 明碼記錄授權對象：不藏。使用者有權知道自己拿到的是誰的授權，
            # 而想外流的人看到這個就知道查得出來——嚇阻正是靠「被看見」生效。
            data[MARKER_KEY] = {"licensee": licensee, "id": fp, "note": note}
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        touched.append(p.name)

    return {"fingerprint": fp, "licensee": licensee, "files": touched}


def identify(pack_dir: str) -> Optional[Dict[str, Any]]:
    """讀出一份規則包的指紋（用來比對外流來源）。"""
    p = Path(pack_dir) / "rulepack.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get(MARKER_KEY)
    except json.JSONDecodeError:
        return None


def verify(pack_dir: str, licensee: str, salt: str = "") -> bool:
    """確認一份包確實是發給某個對象的。"""
    got = identify(pack_dir)
    if not got:
        return False
    _, expect = _derive(licensee, salt)
    return got.get("id") == expect
