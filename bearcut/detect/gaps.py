# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""字間卡頓偵測 —— 補上 whisper 與判斷腦都看不到的死角。

為什麼需要這一層：whisper 會自動「美化」口吃，把重複與卡頓吞掉只留乾淨字幕。
所以「因為你還在因為你還在」可能只被寫成一次乾淨的「因為你還在」，
但那段重講的聲音**還在影片裡**。

結果是這種瑕疵同時避開了另外兩層：
- 文字看不出來 → 判斷腦抓不到
- 裡面有聲音（不是靜音）→ silencedetect 抓不到

它只會在 word 級時間戳上露餡：**字與字之間出現異常長的空檔**。
純機械、零成本、可靠。
"""

from typing import List, Optional

from ..rules import load as load_rules


def detect_gap_cuts(segments: List[dict], duration: float,
                    cfg: Optional[dict] = None) -> List[dict]:
    """掃描字與字之間的空檔，超過門檻的當卡頓剪掉。

    跟靜音一樣保留一點自然停頓、兩端各留緩衝。
    回 `[{"start", "end", "type": "gap", "reason"}]`。

    ---
    **設計註記：曾經有「形態二：單字異常長」，已刻意移除。**

    原本還會偵測「whisper 把卡頓的聲音吃進單一個字的長度裡」（例：「在」佔 1.26 秒），
    然後剪掉那個字的長尾巴。但那是在**猜**「長字是不是卡頓」，會誤砍連續講話——
    實測「因為你還停在」被 whisper 錯標成 1.26 秒的「在」，就被誤剪過。

    改由全片靜音偵測（門檻調低）統一處理所有「沒聲音的空白」，不管它落在字裡還是字間。
    真正「有聲音的重講／口吃」則交給重講偵測與判斷腦。
    **不要把這段邏輯加回來。**
    """
    c = cfg or load_rules().section("gap")
    keep = c.get("keep_sec", 0.25)
    pad = c.get("edge_pad_sec", 0.05)

    # ⚠️ 門檻是 keep + pad*2，**不是** min_gap_sec。
    #
    # 這看起來像 bug（設定裡有 min_gap_sec 卻沒用到），但實際行為就是這樣，
    # 而「實際行為」正是既有成品的品質來源。移植時我一度「修正」成
    # max(min_gap_sec, keep + pad*2)，門檻從 0.35 變 0.6，R09 的 2 處卡頓就全抓不到了
    # ——回歸比對才發現。
    #
    # 教訓：移植時看到不合理的地方，先確認那個不合理有沒有被輸出依賴，
    # 不要順手改。要調鬆緊請改 rulepack 的 keep_sec，不要動這裡的公式。
    reserve = keep + pad * 2

    words = []
    for seg in segments:
        for w in seg.get("words", []):
            if w.get("start") is None or w.get("end") is None:
                continue
            words.append(w)
    words.sort(key=lambda w: w["start"])

    cuts = []
    for a, b in zip(words, words[1:]):
        gap = b["start"] - a["end"]
        if gap <= reserve:
            continue
        cut_start = max(0.0, a["end"] + pad + keep / 2)
        cut_end = min(duration, b["start"] - pad - keep / 2)
        if cut_end - cut_start <= 0.05:
            continue
        cuts.append({
            "start": round(cut_start, 3),
            "end": round(cut_end, 3),
            "type": "gap",
            "reason": (f"字間卡頓 {gap:.1f} 秒（「{a['word'].strip()}」與"
                       f"「{b['word'].strip()}」之間，保留 {keep:.1f} 秒）"),
        })

    cuts.sort(key=lambda c_: c_["start"])
    return cuts


def drop_overlapping(gap_cuts: List[dict], silence_cuts: List[dict]) -> List[dict]:
    """去掉與靜音刀重疊的字間空檔。

    那些其實是真靜音、silencedetect 已經處理過了。留下來的 gap 才是
    「有聲音的卡頓／被吞掉的重講」——silencedetect 看不到的那種。
    """
    out = []
    for g in gap_cuts:
        overlap = any(not (g["end"] <= s["start"] or g["start"] >= s["end"])
                      for s in silence_cuts)
        if not overlap:
            out.append(g)
    return out
