# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""字幕產出：V1 校對檔與 SRT。

## 兩份檔案，兩種用途

- **V1 校對檔**（`_字幕校對_V1.txt`）：純文字、一句一行，**給人讀與改**的。
  使用者拿它校字、挑金句、寫貼文。不含時間碼，因為那會妨礙閱讀。
- **SRT**：給剪輯軟體與平台吃的，時間軸對齊**剪完之後**的成片。

## 時間軸的關鍵：對齊成片，不是原片

字幕的時間必須是「剪完之後」的時間。原片 60 秒處的那句話，若前面剪掉了 8 秒，
在成片裡是 52 秒。`_shift()` 負責這個換算——**算錯會讓整份字幕從某處開始全部偏掉**，
而且越後面偏越多，是最難用眼睛發現的錯。

被剪掉的區間內的字幕直接丟棄（那些話已經不在成片裡了）。
"""

import re
from typing import List, Optional

from .subs import split_rows


def _ts(t: float) -> str:
    """秒 → SRT 時間碼 `HH:MM:SS,mmm`。"""
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{int(round((t - int(t)) * 1000)):03d}"


def _shift(t: float, keep: List[dict]) -> Optional[float]:
    """原片時間 → 成片時間。落在被剪掉的區間內回 None。

    作法：累加這個時間點之前所有保留區間的長度。
    """
    acc = 0.0
    for k in keep:
        if t < k["start"]:
            return None                       # 落在剪掉的洞裡
        if t <= k["end"]:
            return acc + (t - k["start"])
        acc += k["end"] - k["start"]
    return None


def _clamp(t: float, keep: List[dict], forward: bool) -> Optional[float]:
    """把落在剪掉區間裡的時間點，挪到最近的保留邊界。

    `forward=True` 往後找（給段落起點用），`False` 往前找（給段落終點用）。
    """
    direct = _shift(t, keep)
    if direct is not None:
        return direct
    if forward:
        nxt = [k["start"] for k in keep if k["start"] > t]
        return _shift(min(nxt), keep) if nxt else None
    prev = [k["end"] for k in keep if k["end"] < t]
    return _shift(max(prev) - 1e-4, keep) if prev else None


def map_segments(segments: List[dict], keep: List[dict]) -> List[dict]:
    """把辨識段落映射到成片時間軸。

    ## 跨越刀口的段落要「夾到還在的部分」，不能整段丟掉

    先前的作法是「頭或尾落在剪掉區間就整段捨棄」，但**靜音刀經常落在句子中間**
    （句中的自然停頓）。結果是那一整句字幕消失，儘管內容還在成片裡——
    實測 R09 的開場鉤子「一人公司十個月就破千萬」就這樣整句不見。

    正確作法：把起點往後挪、終點往前挪到最近的保留邊界，只有**完全沒有任何部分
    留下來**時才捨棄。
    """
    out = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        s = _clamp(seg["start"], keep, forward=True)
        e = _clamp(seg["end"], keep, forward=False)
        if s is None or e is None or e <= s:
            continue                      # 整段都被剪掉了
        out.append({"start": s, "end": e, "text": text})

    # 夾過之後相鄰段落可能重疊（兩段被夾到同一個邊界），順推避免時間軸倒退
    for a, b in zip(out, out[1:]):
        if b["start"] < a["end"]:
            b["start"] = a["end"]
    return [x for x in out if x["end"] > x["start"]]


def write_srt(mapped: List[dict], path: str, max_len: int = 16) -> str:
    """寫 SRT。每個事件最多兩列，超長的**拆成多個事件而不是截斷**。

    截斷是實務上出過事的做法：整塊文字會被靜默吃掉，而使用者要看片才發現。
    寧可多幾個事件，也不要少幾個字。
    """
    with open(path, "w", encoding="utf-8") as f:
        idx = 1
        for seg in mapped:
            rows = split_rows(seg["text"], max_len=max_len)
            # 每 2 列一個事件，時間按字數比例分配
            chunks = [rows[i:i + 2] for i in range(0, len(rows), 2)] or [[seg["text"]]]
            total_chars = sum(len("".join(c)) for c in chunks) or 1
            t = seg["start"]
            span = max(0.001, seg["end"] - seg["start"])
            for chunk in chunks:
                share = len("".join(chunk)) / total_chars
                end = min(seg["end"], t + span * share)
                f.write(f"{idx}\n{_ts(t)} --> {_ts(end)}\n" + "\n".join(chunk) + "\n\n")
                idx += 1
                t = end
    return path


def write_v1(mapped: List[dict], path: str, title: str = "") -> str:
    """寫 V1 校對檔：一句一行的純文字，給人讀與改。"""
    lines = []
    if title:
        lines += [title, "=" * 40, ""]
    for seg in mapped:
        t = seg["text"].strip()
        if t:
            lines.append(t)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path
