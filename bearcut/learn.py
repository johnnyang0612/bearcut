# -*- coding: utf-8 -*-
"""校正記憶：把人工修過的字幕學起來，同一個錯不要修第二次。

熊董的原話：「之前已經修過的東西不用再修一次了吧」。

作法是拿**人工修正後的字幕**跟**自動產出的字幕**逐字比對，抽出「錯→對」的
對照，存進使用者層級的記憶檔。下一支片校字時就當成已知修正餵給判斷腦。

存在使用者設定目錄而不是規則包裡，有兩個理由：
  1. 規則包是 Pro 每月更新的內容，使用者自己的修正不該被更新覆蓋掉
  2. 每個人講的話不一樣，A 的口頭禪不該變成 B 的替換規則

⚠️ 只學**內容字**，不學標點與長度差異。ASR 每輪分段本來就不同，把那些
   當成修正會累積出一堆假規則，最後把講對的段落改壞。
"""
from __future__ import annotations

import difflib
import json
import pathlib
import re
from typing import Dict, List, Optional, Tuple

# 修正本身的長度上限。太長多半是整句改寫，不該變成替換規則。
# 下限只排除空字串——單字的錯（「愛」→「AI」）正是最典型的辨識錯誤，
# 不能因為短就不學；安全性靠**連上下文一起存**來保證，見 save()。
MIN_LEN = 1
MAX_LEN = 12
# 前後各取多少字當上下文。取太少會誤傷（「跟愛」在別的句子也可能出現），
# 取太多則只認得一模一樣的句子、換一支片就用不上。
CTX = 5

_PUNCT = "，。、！？；：「」『』（）〈〉《》…—·,.!?;:\"'()[]{}~-–— 　\n\t"


def _strip_punct(s: str) -> str:
    return "".join(c for c in s if c not in _PUNCT)


def _srt_text(path: str) -> str:
    """把 SRT 讀成一整串純文字（去標點、去換行）。

    比對用整串而不是逐句——ASR 每輪的分段本來就不同，逐句對齊會全部錯位。
    """
    from .srtlint import parse_srt
    return _strip_punct("".join(s["text"] for s in parse_srt(path)))


def extract(corrected: str, generated: str) -> List[Dict[str, str]]:
    """比對兩份字幕，抽出「錯→對」。回 [{wrong, right, before, after}]。

    `corrected` 是人工修過的（正確答案），`generated` 是自動產出的（可能有錯）。
    """
    right_all = _srt_text(corrected)
    wrong_all = _srt_text(generated)
    if not right_all or not wrong_all:
        return []

    out: List[Dict[str, str]] = []
    sm = difflib.SequenceMatcher(None, wrong_all, right_all, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        wrong, right = wrong_all[i1:i2], right_all[j1:j2]
        # 純新增或純刪除不學：那是 ASR 有沒有聽到的問題，不是聽錯
        if not wrong or not right:
            continue
        if not (MIN_LEN <= len(wrong) <= MAX_LEN
                and MIN_LEN <= len(right) <= MAX_LEN):
            continue
        # 只差在數字寫法（10 vs 十）不學——那是呈現偏好，不是錯字
        if _digits_only_diff(wrong, right):
            continue
        before = wrong_all[max(0, i1 - CTX):i1]
        after = wrong_all[i2:i2 + CTX]
        if len(before) + len(after) < CTX:      # 上下文太少，不可靠
            continue
        out.append({"wrong": wrong, "right": right,
                    "before": before, "after": after})
    return out


def _digits_only_diff(a: str, b: str) -> bool:
    """兩邊只差在阿拉伯數字與中文數字的寫法。"""
    tr = str.maketrans("0123456789", "零一二三四五六七八九")
    return a.translate(tr) == b.translate(tr)


def hotwords_from(corrected: str) -> List[str]:
    """從修正後的字幕撈出專有名詞，當成下一支片的詞彙表。

    抓英文詞（AI、ERP、Podcast）與中英夾雜的詞。中文專有名詞抓不準，
    寧可不抓——詞彙表放錯東西會讓判斷腦把講對的字改壞。
    """
    text = "".join(_srt_text_lines(corrected))
    words = set()
    for m in re.findall(r"[A-Za-z][A-Za-z0-9]{1,15}", text):
        if len(m) >= 2 and m.lower() not in ("the", "and", "for"):
            words.add(m)
    return sorted(words)


def _srt_text_lines(path: str) -> List[str]:
    from .srtlint import parse_srt
    return [s["text"] for s in parse_srt(path)]


def store_path() -> pathlib.Path:
    """記憶檔位置。跟授權碼放同一個地方（使用者層級，不進規則包）。"""
    from .auth import config_dir
    return config_dir() / "corrections.json"


def load() -> Tuple[Dict[str, str], List[str]]:
    """讀記憶。回 `(replacements, hotwords)`，跟規則包的格式一致。"""
    p = store_path()
    if not p.exists():
        return {}, []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}, []
    return (dict(d.get("replacements") or {}), list(d.get("hotwords") or []))


def save(items: List[Dict[str, str]], hotwords: Optional[List[str]] = None
         ) -> Tuple[int, int]:
    """把學到的東西併進記憶檔。回 `(新增修正數, 新增詞彙數)`。

    是併不是覆蓋——記憶要累積，每學一支片就把之前的洗掉等於沒有記憶。
    """
    repl, hot = load()
    n_new = 0
    for it in items:
        # ⚠️ **連上下文一起存**，不要存裸的「錯字→正確字」。
        # 「愛」→「AI」如果存成無條件替換，每支片的「愛情」都會變成「AI情」。
        # 存成「跟愛工作」→「跟AI工作」就只會在該修的地方生效。
        #
        # 那要怎麼一般化到別支片？靠 hotwords：把 AI、ERP 這些專有名詞餵給
        # 判斷腦，它會自己判斷哪個「愛」該寫成 AI——那是語意問題，
        # 本來就該由判斷腦處理，不是字串替換該處理的。
        w = it["before"] + it["wrong"] + it["after"]
        r = it["before"] + it["right"] + it["after"]
        if not it["before"] and not it["after"]:
            continue                      # 沒有上下文就不學，太危險
        if w in repl and repl[w] == r:
            continue
        # 同一段上下文對到不同的正確寫法＝這條不可靠，整條放棄
        if w in repl and repl[w] != r:
            repl.pop(w, None)
            continue
        repl[w] = r
        n_new += 1
    h_new = 0
    for w in (hotwords or []):
        if w not in hot:
            hot.append(w)
            h_new += 1

    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"$說明": "BearCut 校正記憶：人工修過的字幕學起來，同一個錯不修第二次。"
                  "這個檔屬於使用者，不會被規則包更新覆蓋。",
         "replacements": repl, "hotwords": sorted(hot)},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return n_new, h_new
