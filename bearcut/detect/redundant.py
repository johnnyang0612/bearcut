# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""整段重複 / NG take / 廢段偵測。

## 鐵則：判斷腦只回段號，不回時間

判斷腦只回傳「要丟哪幾段」的索引，時間戳一律由我們自己從辨識結果取。
它的時間幻覺動不了真正的切點。

## 確定性防線比 prompt 更重要

prompt 再怎麼調，判斷腦還是會誤判。所以每一種提議都有**程式層的護欄**擋在後面，
對不上就整組不剪。這些門檻不是憑感覺訂的，每一條都對應一個真實的誤剪事故。
"""

import difflib
import json
import re
from typing import Callable, List, Optional

from .. import rules as _rules
from ..llm import FAST, LLMUnavailable, Provider

# ── 保護名單：這兩類內容曾經被誤剪，用確定性 regex 擋住 ─────────────────────
#
# 結尾 CTA：短影音與 Podcast 的「正片結尾」，絕不能被當成收尾廢話。
# R09 實案（2026-07-14）：「歡迎大家留言告訴我…」整段 CTA 被判成收尾寒暄、12 秒全剪。
# 走確定性 regex 不依賴腳本——講者會自由發揮，Podcast 也常常無稿。
CTA_RE = re.compile(
    r"留言|訂閱|追蹤|按讚|按赞|收藏|存起來|存起来|分享|轉發|转发|私訊|私信|傳給|传给"
    r"|告訴我|告诉我|[Pp]odcast|下一集|下集|頻道|频道|連結|链接|置頂|置顶|主頁|主页"
    r"|簡介|简介|小盒子"
)

# 開場鉤子：帶單位的數字爆點詞（十個月／破千萬／99%…）幾乎必是設計過的 hook，不是暖場廢話。
# R09 實案（2026-07-14 重跑）：頭刀把 0~9 秒「一人公司十個月就破千萬…」整個鉤子當開場廢話剪掉。
# 純數字倒數（3 2 1）不帶單位，不會誤中。
HOOK_NUM_RE = re.compile(
    r"(?:破|賺|赚|虧|亏|欠|省|翻)?"
    r"[0-9０-９一二兩两三四五六七八九十百千萬万億亿幾几半\.．]+"
    r"(?:個月|个月|個人|个人|萬|万|億|亿|年|天|週|周|小時|小时|分鐘|分钟|倍|%|％"
    r"|塊|块|元|人|次|支|條|条|集|歲|岁|折)"
)


def is_protected(text: str) -> Optional[str]:
    """這段是不是受保護的內容（CTA 或開場鉤子）。回保護原因或 None。"""
    t = text or ""
    if CTA_RE.search(t):
        return "結尾 CTA"
    if HOOK_NUM_RE.search(t):
        return "開場數字鉤子"
    return None


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def detect_redundant_segments(
    segments: List[dict],
    llm: Provider,
    script_text: Optional[str] = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> List[dict]:
    """用判斷腦找重複／NG／廢段，回 `[{"index", "reason", "kind"}]`（index 為 0-based）。

    沒有判斷腦時回空清單——只靠靜音剪輯，不是失敗。
    """
    def report(p, msg):
        if progress_cb:
            progress_cb(p, msg)

    if not segments or llm is None or not llm.available():
        return []

    numbered = "\n".join(
        f"{i + 1}. [{s['start']:.1f}s-{s['end']:.1f}s] {s['text']}"
        for i, s in enumerate(segments))
    script_section = ""
    if script_text:
        script_section = ("\n拍攝時的腳本逐字稿（判斷哪些是 NG／重複時可對照，"
                          f"講者實際說的跟腳本不同是正常的）：\n{script_text}\n")

    prompt = _rules.load().prompt("redundant", count=len(segments),
                                  numbered=numbered, script_section=script_section)

    report(91, "判斷腦分析重複／口吃／NG 段落中…")
    try:
        data = llm.complete_json(prompt, tier=FAST)
    except LLMUnavailable:
        return []
    if not isinstance(data, dict):
        return []

    notes = data.get("notes", "")
    if notes:
        report(92, f"判斷腦提醒：{notes}")

    def to_idx0(v):
        try:
            i = int(v) - 1
        except (TypeError, ValueError):
            return None
        return i if 0 <= i < len(segments) else None

    def dur(i):
        return segments[i]["end"] - segments[i]["start"]

    result = {}     # idx0 → {reason, kind}；同段被標多次時保留第一個

    # ── 重複組 ────────────────────────────────────────────────────────
    # 保留「後面那次」（前面卡住才重講）。兩道確定性防線擋掉判斷腦亂圈：
    #  ① 極短碎段（< 0.4 秒）是辨識雜訊不是真重講——不當保留對象也不剪它，
    #     避免「保留 0.1 秒碎屑、把完整那段整句剪掉」。
    #  ② 真重複「字句要幾乎一樣」——相似度太低（不同句、只是主題相關）一律不剪。
    for grp in data.get("repeats", []):
        if not isinstance(grp, dict):
            continue
        idxs = sorted({i for i in (to_idx0(s) for s in grp.get("segments", []))
                       if i is not None})
        if len(idxs) < 2:
            continue
        real = [i for i in idxs if dur(i) >= 0.4]
        if len(real) < 2:
            continue        # 夠完整的段不足兩個 → 多半是辨識碎屑，整組不剪

        keep = real[-1]
        keep_text = segments[keep]["text"]
        reason = str(grp.get("reason", "")).strip() or "重複"
        for i in idxs:
            if i == keep or dur(i) < 0.4:
                continue
            if _sim(segments[i]["text"], keep_text) < 0.5:
                continue    # 跟保留段不夠像 → 不是真重複
            result.setdefault(i, {"kind": "repeat",
                                  "reason": f"重複，保留後段第 {keep + 1} 段（{reason}）"})

    # ── 整段重錄 ──────────────────────────────────────────────────────
    # 確定性護欄：cut 與 keep 必須「開頭講同一句」或「結尾收在同一句」
    #（bookend 相似度 ≥ 0.45）——這是整段重錄的鐵證。對不上就整塊不剪。
    for blk in data.get("retake_blocks", []):
        if not isinstance(blk, dict):
            continue
        cut_idx = sorted({i for i in (to_idx0(s) for s in blk.get("cut", []))
                          if i is not None})
        keep_idx = sorted({i for i in (to_idx0(s) for s in blk.get("keep", []))
                           if i is not None})
        if not cut_idx or not keep_idx:
            continue
        if min(keep_idx) <= max(cut_idx):
            continue        # keep 必須整個在 cut 之後

        first_sim = _sim(segments[cut_idx[0]]["text"], segments[keep_idx[0]]["text"])
        last_sim = _sim(segments[cut_idx[-1]]["text"], segments[keep_idx[-1]]["text"])
        if max(first_sim, last_sim) < 0.45:
            continue        # 開頭結尾都對不上 → 不是整段重錄

        reason = str(blk.get("reason", "")).strip() or "整段重錄"
        # 要「整段乾淨剪掉」：cut 的整個跨距每一段都剪，不能只剪判斷腦列的那幾段——
        # 中間漏掉一段會殘留半截句（例「光商業攝影一個單品就」接「你拍一拍照片」語意不明）。
        for i in range(min(cut_idx), max(cut_idx) + 1):
            if dur(i) < 0.2:
                continue
            result.setdefault(i, {"kind": "repeat",
                                  "reason": f"整段重錄，保留後面重講那次（{reason}）"})

    # ── 單獨廢段 ──────────────────────────────────────────────────────
    for item in data.get("junk", []):
        if not isinstance(item, dict):
            continue
        i = to_idx0(item.get("index"))
        if i is None:
            continue
        result.setdefault(i, {"kind": "junk",
                              "reason": str(item.get("reason", "")).strip() or "口吃／NG／廢段"})

    # ── 保護名單：最後一道關 ──────────────────────────────────────────
    # 不管判斷腦怎麼說，CTA 與開場數字鉤子都不剪。
    protected = 0
    for i in list(result):
        why = is_protected(segments[i]["text"])
        if why:
            del result[i]
            protected += 1
    if protected:
        report(92, f"保護名單擋下 {protected} 段（CTA／開場鉤子）")

    out = [{"index": i, "reason": v["reason"], "kind": v["kind"]}
           for i, v in sorted(result.items())]
    report(93, f"判斷腦判斷完成，建議剪掉 {len(out)} 段")
    return out


def detect_adjacent_repeats(segments: List[dict], sim_threshold: float = 0.8,
                            window: int = 3) -> List[dict]:
    """確定性「相鄰整段重複」偵測 —— 不靠判斷腦，100% 可重現。

    掃描相鄰句子，找「幾乎一樣／一句是另一句的開頭（漸進補齊）」的重複組，
    保留最完整那段、剪掉前面。

    **門檻 0.8 是刻意的。** 實測這些對比與排比的相似度都低於它，不會被誤抓：
    - 每天焦慮／每天猶豫　0.50
    - 同一個市場／同一批客人　0.40
    - 撞對了賺一點／撞錯了賠一點　0.71（對↔錯、賺↔賠，是對比不是重複）

    真重複幾乎都是 1.0 或前綴關係，不受影響。這些對比排比交給字級重講偵測或保留。
    """
    n = len(segments)

    def similar(a: str, b: str) -> bool:
        if len(a) >= 3 and len(b) >= 3 and (b.startswith(a) or a.startswith(b)
                                            or a in b or b in a):
            return True
        return _sim(a, b) >= sim_threshold

    drops = {}
    i = 0
    while i < n:
        anchor = segments[i]["text"].strip()
        group = [i]
        if len(anchor) >= 2:
            j = i + 1
            while j < n and j - i <= window:
                if similar(anchor, segments[j]["text"].strip()):
                    group.append(j)
                j += 1
        if len(group) >= 2:
            # 留「最完整（最長）」那段，平手取後面——避免「完整段 + 0.1 秒碎屑」時
            # 留到碎屑、把完整那段剪掉。漸進補齊的情況也會正確留到最長的最後一句。
            keep = max(group, key=lambda k: (segments[k]["end"] - segments[k]["start"], k))
            for k in group:
                if k != keep and not is_protected(segments[k]["text"]):
                    drops[k] = f"相鄰整段重複，保留最完整的第 {keep + 1} 段"
            i = max(group) + 1
        else:
            i += 1

    return [{"index": k, "reason": r, "kind": "repeat"} for k, r in sorted(drops.items())]
