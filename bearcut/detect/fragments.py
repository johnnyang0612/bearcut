# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""破碎片段偵測 —— 字級的口誤碎屑。

## 這層抓的東西，其他層都抓不到

實測一支 98 秒的口播，真正該剪的「重複」全部是這種等級的東西：

    剪沒講完的「也不他」        0.32 秒
    口吃疊字「啊啊」
    「所以以前」聽成「以以前」的多餘那個「以」    0.2 秒，一個字
    純語助詞填充                0.1 秒

段落級的判斷（`redundant.py`）在這種粒度上會誠實地回報「沒有重複」——因為
whisper 段落裡確實沒有。這些碎屑只存在於 **Paraformer 原始逐字流**裡。

## 範圍刻意縮得很小

只挑「明顯破碎的字組」，不碰完整通順的句子。範圍放寬就會像自由判斷那樣暴衝過剪，
而這層剪的東西又短又碎，誤剪很難用眼睛發現。

## 乾淨稿參照：這層最重要的防線

Paraformer 會把「現在戰場長怎麼樣」聽成「现在这常长怎么样」，
「常长」看起來像亂碼，但對照校正過的乾淨稿就知道那是「場長」、是真內容。

沒有這個參照，判斷腦會把一堆**聽錯但完整連貫**的字當碎屑剪掉。
"""

import re
from typing import Callable, List, Optional

from .. import rules as _rules
from ..asr.paraformer import align_cut_to_text
from ..llm import FAST, LLMError, Provider

# 破碎片段一定很短。超過這個長度多半是圈到正常句子了。
MAX_FRAG_LEN = 8

# 單獨出現時一律保留的語助詞與連接詞。
# 剪掉它們會讓畫面一直跳接，比留著更難看。只有疊字（然後然後）才算破碎。
FILLERS = {"然後", "那", "就", "啊", "欸", "嗯", "喔", "唉",
           "那個", "這個", "其實", "的", "了", "對", "嘿", "哦"}


def detect_broken_fragments(
    verbatim_lines: List[dict],
    chars: List[dict],
    llm: Provider,
    clean_ref: Optional[str] = None,
    progress_cb: Optional[Callable] = None,
) -> List[dict]:
    """在原始逐字流裡挑破碎口誤片段。

    `verbatim_lines` 是 Paraformer 字流切出來的短句（`chars_to_lines`），
    `clean_ref` 是校正後的乾淨稿（見模組說明的防線）。
    """
    def report(p, msg):
        if progress_cb:
            progress_cb(p, msg)

    if not verbatim_lines or llm is None or not llm.available():
        return []

    numbered = "\n".join(f"{i + 1}. [{l['start']:.2f}s] {l['text']}"
                         for i, l in enumerate(verbatim_lines))

    ref_section = ""
    if clean_ref:
        ref_section = f"""
## 乾淨內容參考

這支片實際講的內容、用字正確的版本。用來分辨「聽錯字」與「真破碎片段」：

{clean_ref}

⚠️⚠️ **最重要的一條**：上面的原始逐字稿是語音直接轉的，
**很多字是「聽錯」但其實是好內容**。

判斷某幾個字是不是破碎片段時，先到這份參考裡找——
**如果那幾個怪字在參考裡對得上一段「連貫、完整、講得通」的內容
（只是被聽成別的字），那就是好內容，絕對不要剪！**

例：原始稿「现在这常长怎么样」的「常长」看起來像亂碼，
但參考裡是「現在戰場長怎麼樣」→「常长」＝「場長」是真內容 → **不剪**。

只有「在參考裡也找不到對應、確實是卡住吐出來的多餘碎屑（如還才、這這這）」才剪。
"""

    prompt = _rules.load().prompt("fragments", numbered=numbered,
                                  ref_section=ref_section)

    report(93, "偵測破碎口誤片段中…")
    try:
        data = llm.complete_json(prompt, tier=FAST)
    except LLMError:
        return []                       # 這層失敗不影響其他層
    if not isinstance(data, dict):
        return []

    cuts, skipped = [], 0
    for f in (data.get("fragments") or []):
        if not isinstance(f, dict):
            continue
        try:
            li = int(f.get("line", 0)) - 1
        except (TypeError, ValueError):
            continue
        if not (0 <= li < len(verbatim_lines)):
            continue

        frag = str(f.get("text", "")).strip()
        if not frag or len(frag) > MAX_FRAG_LEN:
            skipped += 1
            continue                    # 太長 → 八成圈到正常句子
        if frag in FILLERS:
            skipped += 1
            continue                    # 單獨語助詞不剪（見 FILLERS 說明）

        line = verbatim_lines[li]
        # 鐵則：不信判斷腦的秒數，只信它「剪哪幾個字」，時間由字級時間戳反查
        aligned = align_cut_to_text(line["start"], line["end"], frag, chars, pad=0.6)
        if not aligned:
            skipped += 1
            continue                    # 對不到字 → 寧可不剪

        s, e = aligned
        reason = str(f.get("reason", "")).strip()
        cuts.append({
            "start": round(s, 3), "end": round(e, 3), "type": "repeat",
            "reason": f"語意不通的破碎片段「{frag}」" + (f"（{reason}）" if reason else ""),
        })

    if cuts or skipped:
        report(93, f"破碎片段：採用 {len(cuts)} 處"
                   + (f"，濾掉 {skipped} 處（太長／語助詞／對不到字）" if skipped else ""))
    return cuts
