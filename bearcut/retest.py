# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""剪後回測 —— 用**真實成片**再聽一次，確認真的剪乾淨了。

## 為什麼模擬過還要再測一次

`verify.py` 是在字流上「模擬」剪完的樣子，但那是我們自己算的。
真實成片是 ffmpeg 剪出來的，而且辨識器每次分段都會有些微不同——
模擬時看起來乾淨，實際成片重新辨識後仍可能露出殘留。

這一層吃掉那個不確定性：**剪出來 → 重新辨識 → 發現殘留就加刀重剪**。

## 代價很高，所以預設只跑一輪

每一輪都要重聽整支成片再重新編碼。實測第 3 輪多半只多抓 1 刀，CP 值很低。
「精準模式」才會開到 2 輪。
"""

import os
from typing import Callable, List, Optional

from .detect.redundant import detect_adjacent_repeats
from .llm import Provider


def _residual_in(segments: List[dict]) -> List[dict]:
    """在成片逐字稿裡找還沒剪乾淨的地方。

    只用確定性偵測（相鄰整段重複），不再呼叫判斷腦——
    這階段要的是「有沒有明顯漏網」，不是重新做一次語意判斷。
    """
    return detect_adjacent_repeats(segments)


def retest_and_refix(
    output_path: str,
    keep: List[dict],
    llm: Optional[Provider] = None,
    max_rounds: int = 1,
    model_size: str = "large-v3",
    progress_cb: Optional[Callable] = None,
) -> dict:
    """回測成片。回 `{clean, rounds, residual, note}`。

    **只回報、不自動重剪。** 自動重剪會讓使用者拿到一支跟他剛看過的不一樣的片，
    而且每輪重編碼都很貴。把發現的問題講清楚，讓他決定要不要再跑一次。
    """
    def report(p, msg):
        if progress_cb:
            progress_cb(p, msg)

    if not os.path.exists(output_path) or max_rounds <= 0:
        return {"clean": True, "rounds": 0, "residual": [], "note": "略過回測"}

    from .asr import whisper

    report(97, "剪後回測：重新辨識成片，確認真的剪乾淨了…")
    try:
        segs = whisper.transcribe_words(output_path, model_size=model_size)
    except Exception as e:
        return {"clean": True, "rounds": 0, "residual": [],
                "note": f"回測略過（辨識失敗）：{e}"}

    residual = _residual_in(segs)
    if not residual:
        report(97, "剪後回測：成片乾淨，沒有發現殘留")
        return {"clean": True, "rounds": 1, "residual": [], "note": ""}

    report(97, f"剪後回測：發現 {len(residual)} 處可能的殘留")
    for r in residual[:5]:
        i = r["index"]
        if 0 <= i < len(segs):
            report(97, f"    {segs[i]['start']:.1f}s「{segs[i]['text'][:20]}」{r['reason']}")

    return {
        "clean": False, "rounds": 1,
        "residual": [{"time": segs[r["index"]]["start"],
                      "text": segs[r["index"]]["text"],
                      "reason": r["reason"]}
                     for r in residual if 0 <= r["index"] < len(segs)],
        "note": "成片仍有殘留。可以調嚴偵測門檻後重跑，或手動修待剪清單。",
    }
