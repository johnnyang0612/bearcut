# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""SRT 出廠檢查。

抓的是「使用者隨手打開字幕就會發現、但我們自己不看就不會知道」的那類毛病。
每一條規則都對應一種**實際發生過**的瑕疵。

**只警告，不擋流程。** 這些多半是辨識層的問題，字幕仍然可用；
攔下來反而讓使用者拿不到東西。把問題講清楚，讓他決定要不要重跑。
"""

import re
from typing import List

# 簡體字殘留：whisper 的 zh 偶爾整片吐簡體。
# 只列高頻字——目的是「偵測到有這個現象」，不是做完整的繁簡對照表。
_SIMPLIFIED = set("这那么个们说话时间还没来对开关问题东西发现实现"
                  "样点书学习开会电脑网络产业务经营销费买卖钱转账"
                  "认为觉得应该里边华语汉语国际长处调查")

# 英文黏字：兩個英文詞被黏成一個（justdoit、LifeBalance）
_GLUED_EN = re.compile(r"\b[a-z]+[A-Z][a-z]+\b|\b[a-z]{8,}\b")

# 跨段孤字：整段只有一到兩個中文字，且不是完整語意單位
_ORPHAN_OK = {"對", "好", "嗯", "是", "欸", "喔", "哦", "啊", "對啊", "是啊",
              "好的", "沒有", "可以", "不會", "真的", "當然"}


def parse_srt(path: str) -> List[dict]:
    """讀 SRT，回 `[{index, start, end, text}]`。"""
    try:
        raw = open(path, encoding="utf-8").read()
    except OSError:
        return []
    blocks = re.split(r"\n\s*\n", raw.strip())
    out = []
    for b in blocks:
        lines = [l for l in b.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        m = re.search(r"([\d:,]+)\s*-->\s*([\d:,]+)", lines[1] if len(lines) > 1 else "")
        if not m:
            continue
        out.append({"index": lines[0].strip(), "start": m.group(1),
                    "end": m.group(2), "text": "\n".join(lines[2:])})
    return out


def lint(path: str) -> List[str]:
    """檢查一份 SRT，回警告清單（空的代表沒問題）。"""
    cues = parse_srt(path)
    if not cues:
        return [f"字幕檔讀不到內容或格式不對：{path}"]

    warns = []

    # 1. 簡體字殘留 —— whisper zh 偶爾整片吐簡體
    simp = {ch for c in cues for ch in c["text"] if ch in _SIMPLIFIED}
    if simp:
        sample = "、".join(sorted(simp)[:8])
        warns.append(f"偵測到 {len(simp)} 個簡體字（{sample}…）。"
                     "whisper 的中文辨識偶爾會整片吐簡體，建議重跑一次。")

    # 2. 英文黏字 —— justdoit / LifeBalance 這類
    glued = set()
    for c in cues:
        for w in _GLUED_EN.findall(c["text"]):
            if len(w) >= 8 or re.search(r"[a-z][A-Z]", w):
                glued.add(w)
    if glued:
        warns.append(f"疑似英文黏字：{'、'.join(sorted(glued)[:5])}。"
                     "辨識常把相鄰英文詞黏在一起，請人工確認。")

    # 3. 跨段孤字 —— 「…解決一個問」/「題」這種被拆開的
    orphans = [c for c in cues
               if 1 <= len(re.sub(r"[^一-鿿]", "", c["text"])) <= 2
               and c["text"].strip() not in _ORPHAN_OK]
    if orphans:
        sample = "、".join(f"「{o['text'].strip()}」" for o in orphans[:4])
        warns.append(f"有 {len(orphans)} 段只有一兩個字（{sample}）。"
                     "可能是句子被切斷，請確認是不是完整語意。")

    # 4. 時間軸倒退或重疊
    def to_sec(ts):
        try:
            h, m, rest = ts.split(":")
            s, ms = rest.split(",")
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
        except (ValueError, AttributeError):
            return -1.0

    bad_time = 0
    for a, b in zip(cues, cues[1:]):
        if to_sec(b["start"]) < to_sec(a["end"]) - 0.001:
            bad_time += 1
    if bad_time:
        warns.append(f"有 {bad_time} 處時間軸重疊或倒退，播放器可能顯示異常。")

    # 5. 空白字幕
    empty = sum(1 for c in cues if not c["text"].strip())
    if empty:
        warns.append(f"有 {empty} 段是空的。")

    return warns


def report(path: str, progress_cb=None) -> List[str]:
    """檢查並回報。回警告清單。"""
    warns = lint(path)
    if progress_cb:
        if warns:
            progress_cb(96, f"字幕檢查發現 {len(warns)} 項提醒：")
            for w in warns:
                progress_cb(96, f"    · {w}")
        else:
            progress_cb(96, "字幕檢查：沒有發現問題")
    return warns
