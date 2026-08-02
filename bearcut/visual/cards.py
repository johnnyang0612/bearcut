# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""大字卡 —— 把金句放大打在畫面上。

## 為什麼卡片放上方

臉在畫面中央。字卡壓在臉上會遮住表情，而表情是說服力的一部分。
放上方（頂帶 0~390）既避開臉，又在觀眾視線第一落點。

## 字卡不是字幕

字幕是逐句跟著講的內容走；字卡是**挑出來的重點**，一支片只有幾張。
放太多就跟字幕沒兩樣，反而稀釋了重點。
"""

import json
from typing import Callable, List, Optional

from .. import rules as _rules
from ..llm import FAST, LLMError, Provider
from .style import ANCHORS, PALETTE, TYPE, clean_text, fit_size, style_line, ts

MAX_CARDS_PER_MIN = 2.0     # 每分鐘最多幾張——放太多就變字幕了


def styles() -> List[str]:
    """字卡用的 ASS 樣式。"""
    return [
        style_line("CardTop", TYPE["card_top_size"], PALETTE["white"],
                   outline=6.0, shadow=2.5, align=8, margin_v=0),
        style_line("CardKey", TYPE["card_key_size"], PALETTE["yellow"],
                   outline=7.0, shadow=3.0, align=8, margin_v=0),
    ]


def pick(segments: List[dict], llm: Provider, duration: float,
         progress_cb: Optional[Callable] = None) -> List[dict]:
    """讓判斷腦挑金句並寫成字卡。

    回 `[{start, end, top, key}]`——`top` 是問題或情境句（白字），
    `key` 是重點詞（黃字大字）。

    **時間戳由段號反查，不信判斷腦回的秒數**（全系統鐵則）。
    """
    def report(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not segments or llm is None or not llm.available():
        return []

    want = max(2, min(int(duration / 60 * MAX_CARDS_PER_MIN) + 1, 8))
    numbered = "\n".join(f"{i + 1}. {s['text']}" for i, s in enumerate(segments))

    try:
        prompt = _rules.load().prompt("cards", count=want, numbered=numbered)
    except Exception:
        return []

    report(96, f"挑金句做大字卡（目標 {want} 張）…")
    try:
        data = llm.complete_json(prompt, tier=FAST)
    except LLMError:
        return []
    if not isinstance(data, dict):
        return []

    cards = []
    for c in (data.get("cards") or [])[:want]:
        if not isinstance(c, dict):
            continue
        try:
            i = int(c.get("segment", 0)) - 1
        except (TypeError, ValueError):
            continue
        if not (0 <= i < len(segments)):
            continue
        top = clean_text(c.get("top", ""))
        key = clean_text(c.get("key", ""))
        if not (top or key):
            continue
        seg = segments[i]
        cards.append({
            "start": seg["start"],
            # 字卡停留時間：跟著那一句，但至少 1.5 秒才看得完
            "end": max(seg["end"], seg["start"] + 1.5),
            "top": top[:18], "key": key[:10],
        })

    report(96, f"大字卡：{len(cards)} 張")
    return cards


def events(cards: List[dict], w: int = 1080) -> List[str]:
    """把字卡轉成 ASS 事件行。"""
    cx = w // 2
    # 彈入：從 88% 放大到 104% 再回 100%，比直接淡入更抓眼睛
    pop = ("\\fscx88\\fscy88\\t(0,130,\\fscx104\\fscy104)"
           "\\t(130,210,\\fscx100\\fscy100)\\fad(120,140)")
    out = []
    for c in cards:
        st, en = ts(c["start"]), ts(c["end"])
        if c.get("top"):
            fs = fit_size(c["top"], TYPE["card_top_size"], 960)
            out.append(f"Dialogue: 1,{st},{en},CardTop,,0,0,0,,"
                       f"{{\\pos({cx},{ANCHORS['card_top_y']})\\fs{fs}{pop}}}{c['top']}")
        if c.get("key"):
            fs = fit_size(c["key"], TYPE["card_key_size"], 960)
            out.append(f"Dialogue: 2,{st},{en},CardKey,,0,0,0,,"
                       f"{{\\pos({cx},{ANCHORS['card_key_y']})\\fs{fs}{pop}}}{c['key']}")
    return out
