# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""字幕斷行 —— **永遠不在詞的中間換行**。

## 為什麼不能按字數硬切

「我們要先確認客戶的需求」按 8 字硬切會變成：

    我們要先確認客戶
    的需求

「客戶」被拆開、「的」跑到下一行開頭，讀起來立刻卡住。中文沒有空格，
所以斷行位置必須靠斷詞決定，不能靠計數。

作法：用 jieba 斷詞，貪婪地把詞塞進當前列，塞不下才換列——
**只在詞與詞之間換行**。單一個詞就超過寬度時才允許硬切（那種詞本來就少見）。

jieba 沒裝時退回按標點與字數切，品質差一點但不會壞掉。
"""

import re
from typing import List

# 中文標點：在這些之後斷行最自然，優先於一般詞界
_PUNCT = "。！？；，、：）」』》】…—"

# 不該出現在行首的虛詞。
#
# jieba 會把「的」「怎麼」「做」切成獨立的詞，所以純靠詞界斷行雖然「沒切壞詞」，
# 排出來仍可能是「…確認客戶 / 的需求…」——語法上合法，讀起來卡住。
# 實務字幕不會讓虛詞當行首，這一關是排版品質與「只是沒切錯」的差別。
_WEAK_HEAD = ("的", "了", "是", "在", "和", "與", "跟", "就", "都", "也",
              "而", "但", "卻", "把", "被", "給", "對", "從", "到", "向",
              "地", "得", "過", "呢", "嗎", "吧", "啊", "喔", "耶")

# 不該出現在行尾的字（懸尾）：連接詞留在行尾會讓下一行來得突兀
_WEAK_TAIL = ("和", "與", "跟", "或", "把", "被", "給", "對", "從", "向", "讓")

_jieba = None
_jieba_tried = False


def _get_jieba():
    global _jieba, _jieba_tried
    if not _jieba_tried:
        _jieba_tried = True
        try:
            import jieba
            jieba.setLogLevel(60)          # 關掉「Building prefix dict」那串雜訊
            _jieba = jieba
        except ImportError:
            _jieba = None
    return _jieba


def _cut(jb, text: str) -> List[str]:
    """對繁體文字斷詞。

    ## 為什麼要繞路轉簡體

    jieba 隨 wheel 出貨的只有**簡體詞典**（繁體的 `dict.txt.big` 不在裡面）。
    直接餵繁體會出事：
    - `HMM=False`：繁體詞不在詞典裡 → 整句碎成單字（我/們、確/認、營/收）
    - `HMM=True`：靠統計猜未登錄詞 → 猜錯就切出「先確 / 認客戶」這種假詞

    對一個定位在繁體中文的工具，這是不能接受的。

    ## 解法：借簡體詞典，只取「切在哪裡」

    先轉簡體斷詞，拿到每個詞的**長度**，再用那些長度去切**原始的繁體文字**。
    繁簡絕大多數是 1:1 字元對應，位置可直接套用；不是 1:1 的（如 著/着 合併）
    會讓總長度對不上，此時退回直接斷繁體，寧可切得差一點也不要錯位。

    這樣不必多下載 8MB 詞典，也不必轉換輸出的文字——**轉換只發生在斷詞這一步，
    使用者拿到的永遠是原始繁體**。
    """
    try:
        from zhconv import convert
        simp = convert(text, "zh-cn")
    except ImportError:
        simp = text

    if len(simp) != len(text):
        # 繁簡長度不一致 → 位置對不上，不能套用，直接斷原文
        return list(jb.cut(text))

    out, pos = [], 0
    for w in jb.cut(simp):
        n = len(w)
        if n:
            out.append(text[pos:pos + n])
            pos += n
    if pos < len(text):
        out.append(text[pos:])
    return [w for w in out if w]


def _tokens(text: str) -> List[str]:
    """把文字切成「不可再分」的單位。"""
    jb = _get_jieba()
    if jb is None:
        # 退回：以標點為界，其餘逐字。品質差但不會把詞切壞得太離譜
        out, cur = [], ""
        for ch in text:
            cur += ch
            if ch in _PUNCT:
                out.append(cur)
                cur = ""
        if cur:
            out.extend(list(cur))
        return out

    toks = []
    for w in _cut(jb, text):
        w = w.strip()
        if not w:
            continue
        # 標點黏在前一個詞後面——標點自己一列很難看
        if w[0] in _PUNCT and toks:
            toks[-1] += w
        else:
            toks.append(w)
    return toks


def split_rows(text: str, max_len: int = 16) -> List[str]:
    """把一句話切成多列，每列不超過 max_len 字，且不切斷詞。

    回列的清單（至少一列）。
    """
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    # 以「詞的清單」而非字串累積，換列時才還有詞界資訊可用（見下方虛詞處理）
    rows: List[List[str]] = []
    cur: List[str] = []

    def flush():
        if cur:
            rows.append(list(cur))
            cur.clear()

    for tok in _tokens(text):
        # 單一個詞就超寬 → 只好硬切（罕見）
        if len(tok) > max_len:
            flush()
            for i in range(0, len(tok), max_len):
                rows.append([tok[i:i + max_len]])
            continue

        width = sum(len(t) for t in cur)
        if width + len(tok) <= max_len:
            cur.append(tok)
        else:
            # 要換列了。若新列會以虛詞開頭（「的需求…」），把目前這列的最後一個詞
            # 一起帶下去，讓虛詞有東西可以依附。前提是帶下去仍放得下、且這列還有剩。
            if tok.startswith(_WEAK_HEAD) and len(cur) > 1:
                carry = cur[-1]
                if len(carry) + len(tok) <= max_len:
                    cur.pop()
                    flush()
                    cur.extend([carry, tok])
                else:
                    flush()
                    cur.append(tok)
            else:
                flush()
                cur.append(tok)

        # 標點結尾是天然的斷點，趁機換列讀起來最順
        if cur and cur[-1][-1] in _PUNCT and sum(len(t) for t in cur) >= max_len * 0.6:
            flush()

    flush()
    return _polish(["".join(r) for r in rows], max_len) or [text]


def _polish(rows: List[str], max_len: int) -> List[str]:
    """修掉虛詞行首與懸尾。

    只在**不超寬、不弄丟字**的前提下搬動——排版是加分，正確性是底線。
    """
    if len(rows) < 2:
        return rows

    out = list(rows)
    for i in range(1, len(out)):
        prev, cur = out[i - 1], out[i]
        if not prev or not cur:
            continue

        # 行首是虛詞 → 往前一列搬（前一列要放得下）
        for w in _WEAK_HEAD:
            if cur.startswith(w) and len(prev) + len(w) <= max_len:
                out[i - 1] = prev + w
                out[i] = cur[len(w):]
                break

        # 前一列以連接詞收尾 → 把它推到這一列開頭（這一列要放得下）
        prev = out[i - 1]
        if prev and prev[-1] in _WEAK_TAIL and len(out[i]) + 1 <= max_len:
            out[i] = prev[-1] + out[i]
            out[i - 1] = prev[:-1]

    return [r for r in out if r]
