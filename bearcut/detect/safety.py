# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""暴走安全閥 —— 全系統最重要的防呆。

## 為什麼需要

每一層偵測都可能失控：判斷腦誤判、素材特殊、模型當天狀況不好。
一次暴走就能把整支片剪爛，而使用者往往要到看片才發現。

## 設計原則：**獨立預算**

安全閥是**逐層**檢查的，不是總量檢查。某一層暴走時只作廢**那一層**，
其他穩定的層照常生效。

這點很重要：確定性偵測（靜音、字間空檔）幾乎不會出錯，
而判斷腦那幾層才是變數。若用總量控管，一層暴走會連帶廢掉全部好刀，
使用者反而拿到一支「幾乎沒剪」的片，比誤剪更難察覺。

## ⚠️ 只套在「會產生幻覺的層」，不要套在確定性層

適用：A/B diff、重講判斷、破碎片段、判斷腦提議 —— 這些會誤判。

**不適用：靜音、字間卡頓。** 它們是純訊號處理，不會暴走，數量多只代表素材本身
停頓多。實測 R09（97 秒口播）有 38 段靜音是正常的，套上 25 刀上限會把整層作廢，
使用者反而拿到一支沒剪的片。

判準很簡單：**這一層的結果會不會因為模型當天狀況不同而改變？** 會 → 要閥；不會 → 不要。

## 作廢的提議要留檔，不是丟掉

被擋下的提議寫進 `*_暴走作廢.json`，供人工或高階模型複審。
有些素材（例如錄製時卡住就直接重講）本來就有密集的真重講，
被擋下不代表判斷錯——留檔才能事後調門檻重跑，而不是永遠不知道自己漏了什麼。
"""

import json
import os
from typing import List, Optional, Tuple

from ..rules import load as load_rules


def total_seconds(cuts: List[dict]) -> float:
    return sum(max(0.0, float(c["end"]) - float(c["start"])) for c in cuts)


def check(cuts: List[dict], duration: float, layer: str = "",
          max_ratio: Optional[float] = None,
          max_count: Optional[int] = None) -> Tuple[bool, str]:
    """檢查某一層的提議是否暴走。

    回 `(ok, reason)`。`ok=False` 時呼叫端應作廢**這一層**（並留檔），
    但保留其他層。
    """
    if not cuts or duration <= 0:
        return True, ""

    r = load_rules()
    if max_ratio is None:
        max_ratio = r.get("safety.max_cut_ratio", 0.25)
    if max_count is None:
        max_count = r.get("safety.max_cut_count", 25)

    total = total_seconds(cuts)
    ratio = total / duration

    if ratio > max_ratio:
        return False, (f"{layer}提議剪 {len(cuts)} 刀／{total:.0f} 秒"
                       f"（佔 {ratio * 100:.0f}%，超過 {max_ratio * 100:.0f}% 上限），"
                       f"疑似暴走，作廢這層")
    if len(cuts) > max_count:
        return False, (f"{layer}提議剪 {len(cuts)} 刀"
                       f"（超過 {max_count} 刀上限），疑似暴走，作廢這層")
    return True, ""


def check_fragments(cuts: List[dict], duration: float,
                    layer: str = "破碎片段層") -> Tuple[bool, str]:
    """破碎片段層專用的較嚴門檻。

    這層抓的是半句、雜字這種很短的東西，最容易誤判成一大票刀，
    所以門檻設得比其他層嚴。
    """
    r = load_rules()
    return check(cuts, duration, layer,
                 max_ratio=r.get("safety.fragments_max_cut_ratio", 0.10),
                 max_count=r.get("safety.fragments_max_cut_count", 8))


def archive(cuts: List[dict], path: str, layer: str, reason: str) -> None:
    """把被作廢的提議留檔（append），供複審。

    刻意用 append：同一支影片可能有多層先後被作廢，每一次都要留下來。
    寫檔失敗不該讓整條管線中斷——留檔是輔助，不是主流程。
    """
    if not cuts:
        return
    record = {"layer": layer, "reason": reason, "count": len(cuts),
              "seconds": round(total_seconds(cuts), 3), "cuts": cuts}
    try:
        existing = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if not isinstance(existing, list):
                existing = [existing]
        existing.append(record)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except (OSError, json.JSONDecodeError):
        pass


def guard(cuts: List[dict], duration: float, layer: str,
          voided_path: Optional[str] = None,
          fragments: bool = False,
          report=None) -> List[dict]:
    """一站式：檢查 → 過關就原樣回傳，暴走就留檔並回空清單。

    這是各偵測層的標準收尾，呼叫端不必自己記得留檔。
    """
    fn = check_fragments if fragments else check
    ok, reason = fn(cuts, duration, layer) if fragments else check(cuts, duration, layer)
    if ok:
        return cuts
    if report:
        report(93, reason + "（已留檔供複審）")
    if voided_path:
        archive(cuts, voided_path, layer, reason)
    return []
