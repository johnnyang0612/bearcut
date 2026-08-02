# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""Paraformer 中文逐字辨識 —— **全系統所有切點時間的唯一來源**。

為什麼是它而不是 whisper：
- 中文辨識準（通用 wav2vec2 在中文會糊成雜訊）
- **完整保留重複、結巴、贅字** —— whisper 會「順稿」把它們吞掉，
  而那些正是我們要剪的東西。在一份已經把重講吞掉的逐字稿上做出完美時間戳，
  什麼問題都沒解決。
- **字級精準時間戳** —— 切點不必用猜的

實測「現在一頓火鍋的預算就 現在 現在幾餐飯的預算」三段都被原樣保留、時間精準。

缺點：輸出簡體字。但我們只取「結構與時間」，字幕文字另用 whisper 的繁體，不受影響。

## 鐵則
LLM 只決定「剪什麼內容」，**秒數一律由這裡的字級時間戳反查**。
`refine_cut_to_words` / `align_segment_to_chars` / `align_cut_to_text` /
`snap_cuts_off_chars` 就是在做這件事。違反這條會造成誤剪。
"""

import difflib
import os
import re
from typing import List, Optional, Tuple

# ⚠️ 必須在載入任何原生函式庫「之前」設定，所以放在模組最頂端、import funasr 之前。
#
# torch 與 funasr 各自帶一份 Intel OpenMP（Windows 是 libiomp5md.dll、macOS 是
# torch/.dylibs 裡的 libomp）。同一個程序二次載入時 OpenMP runtime 會 abort，
# 導致 **整個 Python 直接 segfault（exit 139），連 traceback 都沒有**。
# 崩點在 funasr AutoModel 建構，不分 CPU/GPU、與輸入格式無關（2026-07-19 實查）。
# KMP_DUPLICATE_LIB_OK 讓第二份 OpenMP 靜默共存即可解。Windows 與 macOS 同一個解法。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")

_MODEL = {}


def _get_model():
    """建立並快取 FunASR 模型。"""
    if "m" in _MODEL:
        return _MODEL["m"]

    # faster-whisper(ctranslate2) 與 funasr(torch) 在同一程序會搶 cuDNN：
    # whisper 先載了自己的 cuDNN，Paraformer 走 torch 的 cuDNN 就找不到符號
    # （cudnnGetLibConfig / Error code 127）而整個程序崩掉。
    # 關掉 torch 的 cuDNN 即可，whisper 走 ct2 不受影響。
    try:
        import torch
        torch.backends.cudnn.enabled = False
    except Exception:
        pass

    from funasr import AutoModel

    # 2026-07-19：本機 GPU 跑 Paraformer 會原生 segfault（exit 139）——
    # whisper(ct2) 先占了 GPU/cuDNN，funasr(torch) 再上去在 co-residence 下直接把程序打死。
    # segfault 不是 Python 例外，try/except 退回 CPU 攔不到（GPU 一 build 就死）。
    # 且單獨測 GPU 還會冒幻影 OOM（10GB 空閒卻配不出 20MiB）。
    # → 預設鎖 CPU：實測 CPU + KMP_DUPLICATE_LIB_OK 穩過，107 秒的片子只要數十秒，
    #   字級時間戳照舊精準。健康機器要回 GPU 就設 PARAFORMER_DEVICE=cuda。
    #
    # macOS 同樣預設 CPU：Paraformer 單核就有約 10 倍實時，MPS 帶來的風險大於效益。
    device = os.environ.get("PARAFORMER_DEVICE", "cpu")

    # 一定要帶 VAD：先把長音訊切成短句再逐段辨識。否則整包硬解會在分塊接縫
    # 產生「把字打兩遍」的瑕疵（品品底底不不能做）。帶 VAD 後乾淨，又保留真正的重講。
    _MODEL["m"] = AutoModel(
        model="paraformer-zh", vad_model="fsmn-vad",
        disable_update=True, disable_pbar=True, log_level="ERROR",
        device=device,
    )
    return _MODEL["m"]


def transcribe_chars(path: str, progress_cb=None) -> List[dict]:
    """整支音訊逐字辨識（走 VAD 切句），回 `[{char, start, end}]`（絕對秒）。"""
    if progress_cb:
        progress_cb(88, "Paraformer 中文逐字辨識中（VAD 切句）…")
    m = _get_model()
    res = m.generate(input=path, batch_size_s=300)

    chars = []
    for r in res or []:
        text = (r.get("text") or "").replace(" ", "")
        ts = r.get("timestamp") or []
        for ch, span in zip(text, ts):
            try:
                a, b = span
            except (TypeError, ValueError):
                continue
            chars.append({"char": ch,
                          "start": round(a / 1000.0, 3),
                          "end": round(b / 1000.0, 3)})
    chars.sort(key=lambda c: c["start"])
    return chars


def chars_to_lines(chars: List[dict], gap_split: float = 0.25,
                   max_chars: int = 14) -> List[dict]:
    """把逐字切成短句給判斷層讀。

    依「字間空檔 > gap_split」或「累積字數 >= max_chars」斷句，確保每句夠短、
    時間夠細——判斷層才能給出精準的剪輯位置，而不是一大段看不清哪裡該剪。
    """
    lines, cur = [], []
    for c in chars:
        if cur and (c["start"] - cur[-1]["end"] > gap_split or len(cur) >= max_chars):
            lines.append(_mk_line(cur))
            cur = []
        cur.append(c)
    if cur:
        lines.append(_mk_line(cur))
    return lines


def _mk_line(group: List[dict]) -> dict:
    return {"start": group[0]["start"], "end": group[-1]["end"],
            "text": "".join(c["char"] for c in group)}


def _gap_midpoints(first: dict, last: dict, chars: List[dict]) -> Tuple[float, float]:
    """把切點放到「字與字之間的空隙中點」，不咬到前後要保留的字。"""
    prev = [c for c in chars if c["end"] <= first["start"] + 0.01]
    nxt = [c for c in chars if c["start"] >= last["end"] - 0.01]
    s = (prev[-1]["end"] + first["start"]) / 2 if prev else first["start"]
    e = (last["end"] + nxt[0]["start"]) / 2 if nxt else last["end"]
    return s, e


def refine_cut_to_words(cut_start: float, cut_end: float,
                        chars: List[dict]) -> Tuple[float, float]:
    """把一刀的邊界對齊到「要剪的字」的範圍，切點落在字間空隙中點。

    找不到對應字就原樣回傳。
    """
    if not chars:
        return cut_start, cut_end
    # 「主要落在這刀範圍內」的字（字的中點在 [start, end] 內）
    inside = [c for c in chars if cut_start <= (c["start"] + c["end"]) / 2 <= cut_end]
    if not inside:
        return cut_start, cut_end
    s, e = _gap_midpoints(inside[0], inside[-1], chars)
    if e <= s:
        return cut_start, cut_end
    return round(s, 3), round(e, 3)


def align_segment_to_chars(seg_text: str, seg_start: float, seg_end: float,
                           chars: List[dict], pad: float = 0.9) -> Optional[Tuple[float, float]]:
    """用「whisper 段文字」在 Paraformer 字流裡找出它**實際**的精準時間範圍。

    為什麼需要：whisper 的段時間戳有時會偏。實測「你隨便按按」whisper 標
    [90.9, 91.9]，但 Paraformer 顯示真正的位置在 91.7 之後。直接拿 whisper 的
    段時間去剪，會剪到旁邊的字（把「不問原因」的「因」吃掉）。

    繁簡相容：用 difflib 找共同字當錨（你/便/按 繁簡相同，只有 隨/随 不同也對得到）。

    回 (start, end)；對不到回 None，呼叫端應退回 `refine_cut_to_words`。
    """
    seg = re.sub(r"\s", "", seg_text or "")
    if not seg or not chars:
        return None
    lo, hi = seg_start - pad, seg_end + pad
    win = [c for c in chars if lo <= (c["start"] + c["end"]) / 2 <= hi]
    if not win:
        return None

    wtext = "".join(c["char"] for c in win)
    sm = difflib.SequenceMatcher(None, wtext, seg)
    blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
    # 共同字太少（< 40% 段長）→ 對不準，放棄，交回呼叫端用時間法
    if not blocks or sum(b.size for b in blocks) < max(2, int(len(seg) * 0.4)):
        return None

    first = win[blocks[0].a]
    last = win[blocks[-1].a + blocks[-1].size - 1]
    s, e = _gap_midpoints(first, last, chars)
    if e <= s:
        return None
    return round(s, 3), round(e, 3)


def align_cut_to_text(cut_start: float, cut_end: float, removed_text: str,
                      chars: List[dict], pad: float = 0.5) -> Optional[Tuple[float, float]]:
    """用「LLM 回報的要剪文字」+ 字級精準時間，精準對齊一刀。

    為什麼需要：LLM 只看得到句級時間，回的秒數常多估或少估。
    所以**不信它的秒數**，只信它「剪哪幾個字」——在它給的時間範圍附近找出與
    removed_text 相符的那串字，用那串字的精準起訖當切點。

    回 (start, end)；對不上回 None，呼叫端應退回 `refine_cut_to_words`。
    """
    removed = (removed_text or "").strip()
    if not removed or not chars:
        return None

    # 候選窗：時間範圍前後各放寬 pad 秒，容忍 LLM 的時間誤差
    lo = next((i for i, c in enumerate(chars)
               if (c["start"] + c["end"]) / 2 >= cut_start - pad), None)
    hi = next((i for i in range(len(chars) - 1, -1, -1)
               if (chars[i]["start"] + chars[i]["end"]) / 2 <= cut_end + pad), None)
    if lo is None or hi is None or hi < lo:
        return None

    window = chars[lo:hi + 1]
    idx = "".join(c["char"] for c in window).find(removed)
    if idx < 0:
        return None

    s, e = _gap_midpoints(window[idx], window[idx + len(removed) - 1], chars)
    if e <= s:
        return None
    return round(s, 3), round(e, 3)


def snap_cuts_off_chars(cuts: List[dict], chars: List[dict],
                        min_keep_gap: float = 0.12) -> List[dict]:
    """核對靜音／卡頓刀的位置，確保刀口不會把字削掉一半。

    這些刀原本是用 whisper 字時間或 ffmpeg 靜音算出來的，而 whisper 偶爾把
    「字的結尾算太早」→ 以為後面有大空隙而下刀 → 刀的邊界**插進某個字中間**。
    實測「對不對」的最後一個「對」（63.27~63.51）被起點 63.255 的卡頓刀削掉前半。

    修法：
      1. 起點落在某個字中間 → 往後推到那個字的結尾（不削字尾）
      2. 終點落在某個字中間 → 往前推到那個字的開頭（不削字頭）
      3. 推完幾乎沒剩（< min_keep_gap）＝這刀整段落在連續講話裡，是 whisper 的
         假空隙 → 直接丟掉這刀（Paraformer 證實那裡根本沒有可剪的空隙）
    """
    if not chars:
        return cuts

    out = []
    for c in cuts:
        s, e = float(c["start"]), float(c["end"])
        for ch in chars:                       # 字不重疊，最多命中一個
            if ch["start"] + 0.005 < s < ch["end"] - 0.005:
                s = ch["end"]
                break
        for ch in chars:
            if ch["start"] + 0.005 < e < ch["end"] - 0.005:
                e = ch["start"]
                break
        if e - s < min_keep_gap:
            continue                            # 落在連續講話中的假空隙，不剪

        nc = dict(c)
        nc["start"], nc["end"] = round(s, 3), round(e, 3)
        if (nc["start"], nc["end"]) != (round(float(c["start"]), 3),
                                        round(float(c["end"]), 3)):
            nc["reason"] = (c.get("reason", "")
                            + "（已對齊 Paraformer 字邊界，不削字）").strip()
        out.append(nc)
    return out


def text_of(chars: List[dict]) -> str:
    return "".join(c["char"] for c in chars)
