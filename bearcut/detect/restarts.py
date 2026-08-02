# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""重講偵測 —— 在 Paraformer 原始字流裡找「講到一半重新開始講同一件事」。

## 為什麼要在字流層做
whisper 會把重講順掉，所以從順稿看不出來。Paraformer 完整保留重複，
「你用一十｜你用十天」「現在一頓火鍋的預算就｜現在幾餐飯的預算」這種
在字流裡是原樣存在的。

## 最難的問題：重講 vs 排比

這兩者在機械特徵上幾乎一樣，都是「開頭幾個字相同」：

| | 例子 | 該剪嗎 |
|---|---|---|
| 重講 | 現在一頓火鍋的預算就…**現在**幾餐飯的預算 | ✅ 剪掉第一次 |
| 排比 | **同一個**市場…**同一批**客人 | ❌ 這是修辭，不能剪 |

實測相似度：真重講「現在火鍋」0.55、不同句「基本功給做好」0.56 ——
**機械門檻分不開。** 所以這一層只負責「找候選 + 給精準時間」，
中等相似度的一律交給判斷腦做語意是非題。

只有兩種情況直接剪、不問判斷腦：
1. **截斷重講**：第二次把第一次原樣再講一遍（是數據告 → 是數據告訴你的）
2. **高度相似**：整體相似度 ≥ strong_sim，明顯是同一句重來

剪的範圍是 `[第一次開始, 第二次開始)` —— **剪掉第一次、留第二次**。
"""

import difflib
from typing import List, Optional

from ..rules import load as load_rules


def find_restart_candidates(chars: List[dict], cfg: Optional[dict] = None) -> List[dict]:
    """在字流裡找重講候選。

    回 `[{i, j, L, confident, opening, attempt_text, after_text, start, end}]`：
    - `i`/`j`：第一次與第二次的字索引
    - `confident=True` → 確定性重講，可直接剪
    - `confident=False` → 模糊，需交判斷腦做是非判斷
    """
    c = cfg or load_rules().section("restart")
    min_ngram = c.get("min_ngram", 2)
    max_gap_chars = c.get("max_gap_chars", 16)
    max_attempt_sec = c.get("max_attempt_sec", 3.2)
    short_2gram_sec = c.get("short_2gram_sec", 1.0)
    frame_sim = c.get("frame_sim", 0.5)
    strong_sim = c.get("strong_sim", 0.72)

    n = len(chars)
    cands: List[dict] = []
    i = 0
    while i < n:
        found = None
        for j in range(i + min_ngram, min(i + max_gap_chars + 1, n)):
            # 從 i 與 j 同步往後比，算出共同開頭長度 L
            L = 0
            while i + L < j and j + L < n and chars[i + L]["char"] == chars[j + L]["char"]:
                L += 1
            if L < min_ngram:
                continue

            dur = chars[j - 1]["end"] - chars[i]["start"]
            if dur > max_attempt_sec:
                continue          # 第一次太長，不像「講到一半重來」

            attempt = "".join(x["char"] for x in chars[i:j])
            after_same = "".join(x["char"] for x in chars[j:min(j + (j - i), n)])
            sm = difflib.SequenceMatcher(None, attempt, after_same)
            blocks = sm.get_matching_blocks()
            sim = sm.ratio()

            # 重講：開頭一樣，而且**後面還有一段也一樣**、整體很像同一句
            has_inner = any(b.size >= 2 and b.a > 0 for b in blocks)
            # 截斷重講：第二次開頭把第一次原樣再講一遍
            is_truncated = len(after_same) >= 2 and after_same == attempt
            short = dur <= short_2gram_sec

            # 確定重講 → 直接剪，不問判斷腦
            strong = is_truncated or (has_inner and sim >= strong_sim)
            # 模糊 → 交判斷腦語意判斷（第一次是完整句就保留、半句沒講完才剪）
            weak = (has_inner and sim >= frame_sim) or short

            if not (strong or weak):
                continue          # 長、整體不夠像 → 排比或對比，直接排除

            found = (j, L, strong)
            break

        if found:
            j, L, confident = found
            cands.append({
                "i": i, "j": j, "L": L,
                "confident": confident,
                "opening": "".join(x["char"] for x in chars[i:i + L]),
                "attempt_text": "".join(x["char"] for x in chars[i:j]),
                "after_text": "".join(x["char"] for x in chars[j:min(j + (j - i) + 4, n)]),
                "start": round(chars[i]["start"], 3),
                "end": round(chars[j]["start"], 3),
            })
            i = j                 # 跳過已納入的，避免重疊候選
        else:
            i += 1
    return cands


def candidates_to_cuts(cands: List[dict], confident_only: bool = True) -> List[dict]:
    """把候選轉成刀。預設只取確定性的那批。"""
    cuts = []
    for c in cands:
        if confident_only and not c.get("confident"):
            continue
        if c["end"] <= c["start"]:
            continue
        cuts.append({
            "start": c["start"], "end": c["end"], "type": "restart",
            "reason": f"重講：「{c['attempt_text']}」重新講成「{c['after_text']}」",
        })
    return cuts
