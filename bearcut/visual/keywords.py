# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""關鍵詞上色 —— 讓重點從字幕裡跳出來。

## 為什麼要挑，不能全上

整句都上色等於都沒上色。實務上每句最多挑 1-2 個詞，而且**長片要更克制**——
一支 90 秒的短影音上滿色還算有節奏，一支 20 分鐘的長片就變成整片閃爍。

所以長片的密度收斂到約 1/5。

## 挑什麼

優先序有明確的高低，不是隨便挑「看起來重要的」：

1. **帶單位的數字**（破千萬、十個月、99%）—— 實測最有感，一定上
2. **金額與時間** —— 觀眾會停下來看
3. **對比詞**（不是…而是、以前…現在）—— 語意的轉折點
4. **判斷腦挑的**（規則包 prompt）—— 補上機械規則抓不到的

前三種是確定性的正則，不需要 LLM 也能跑——這是「沒有判斷腦也要能用」的一部分。
"""

import re
from typing import Dict, List, Optional, Tuple

from .style import PALETTE

# 帶單位的數字：最有感的一類，優先上色。
# 純數字（3 2 1 倒數）不帶單位，不會誤中。
NUM_UNIT = re.compile(
    r"(?:破|賺|赚|虧|亏|欠|省|翻|漲|涨|跌)?"
    r"[0-9０-９一二兩两三四五六七八九十百千萬万億亿幾几半\.．]+"
    r"(?:個月|个月|個人|个人|萬|万|億|亿|年|天|週|周|小時|小时|分鐘|分钟|秒"
    r"|倍|%|％|塊|块|元|人|次|支|條|条|集|歲|岁|折|成)"
)

# 對比與轉折：語意的重點所在
CONTRAST = re.compile(
    r"(?:不是|而是|以前|現在|过去|過去|從前|从前|反而|但是|可是|其實|其实"
    r"|關鍵|关键|重點|重点|最重要|唯一|完全|根本|絕對|绝对)"
)

# 這些詞就算符合上面的規則也不上色——太常見，上了只會讓畫面雜亂
STOP = {"這個", "那個", "就是", "然後", "所以", "因為", "如果", "可以", "一個"}


def find(text: str, extra: Optional[List[str]] = None,
         max_per_line: int = 2) -> List[Tuple[int, int]]:
    """在一行字幕裡找要上色的區間，回 `[(start, end)]`（字元索引）。

    `extra` 是判斷腦或規則包提供的額外關鍵詞。
    """
    if not text:
        return []

    spans: List[Tuple[int, int, int, int]] = []   # (優先序, -衝擊力, start, end)
    for m in NUM_UNIT.finditer(text):
        # 帶動詞前綴的數字（破千萬、欠六百萬）比裸數字（十個月）有感得多。
        # 沒有這個加權，「一人公司十個月就破千萬」會挑到「一人」而漏掉「破千萬」——
        # 位置在前的先贏，但最該亮的是後面那個。
        impact = 2 if m.group()[0] in "破賺赚虧亏欠省翻漲涨跌" else 0
        impact += 1 if len(m.group()) >= 3 else 0        # 長一點的通常更具體
        spans.append((0, -impact, m.start(), m.end()))
    for w in (extra or []):
        w = (w or "").strip()
        if len(w) < 2 or w in STOP:
            continue
        i = text.find(w)
        if i >= 0:
            spans.append((1, 0, i, i + len(w)))
    for m in CONTRAST.finditer(text):
        if m.group() not in STOP:
            spans.append((2, 0, m.start(), m.end()))

    # 依「優先序 → 衝擊力 → 位置」排，去掉重疊的
    spans.sort(key=lambda x: (x[0], x[1], x[2]))
    spans = [(p, s, e) for p, _imp, s, e in spans]
    chosen: List[Tuple[int, int]] = []
    for _, s, e in spans:
        if len(chosen) >= max_per_line:
            break
        if any(not (e <= cs or s >= ce) for cs, ce in chosen):
            continue
        chosen.append((s, e))
    return sorted(chosen)


#: 語意色。判斷腦挑詞時只需要說「這是痛點」或「這是亮點」，
#: 對應到哪個色碼由這裡決定——換配色不用動判準。
TONE = {
    "red": PALETTE["red"],        # 痛點、代價、負面：虧、欠、搞死、失敗
    "gold": PALETTE["yellow"],    # 數字、成績、亮點：破千萬、10 個月
    "cyan": PALETTE["cyan"],      # 專有名詞、產品名：AI ERP、AI Native
}
DEFAULT_TONE = "gold"


def colorize(text: str, spans, colour: str = PALETTE["yellow"],
             base: str = PALETTE["white"], pop: bool = True) -> str:
    """把區間套上顏色與放大效果，回 ASS 文字。

    `spans` 可以是 `[(start, end)]`，也可以是 `[(start, end, 語意色)]`——
    後者由判斷腦決定每個詞的語氣（痛點紅、亮點金、專有名詞青）。

    `pop=True` 會讓關鍵詞彈一下（118% → 100%）。放大幅度刻意保守——
    直式字幕本來就貼近安全框邊緣，放太大右緣會壓到互動欄。
    """
    if not spans:
        return text

    out, cur = [], 0
    for sp in spans:
        s, e = sp[0], sp[1]
        c = TONE.get(sp[2], colour) if len(sp) > 2 else colour
        out.append(text[cur:s])
        word = text[s:e]
        if pop:
            out.append(f"{{\\c{c}\\fscx118\\fscy118"
                       f"\\t(0,120,\\fscx100\\fscy100)}}{word}{{\\c{base}}}")
        else:
            out.append(f"{{\\c{c}}}{word}{{\\c{base}}}")
        cur = e
    out.append(text[cur:])
    return "".join(out)


#: 詞被斷行切開時，至少要有這麼多字落在這一列才值得上色。
#: 太短的殘塊上色只會看起來像手滑。
_MIN_PARTIAL = 2


def _locate(row: str, word: str) -> Optional[Tuple[int, int]]:
    """在這一列字幕裡找出這個詞的位置。回 `(start, end)`，找不到回 None。

    ⚠️ 判斷腦是對「整句」挑詞的，但字幕會斷行——「欠了六七百萬」很可能
    被切成「那基本上曾經欠了」＋「六七百萬」兩列，整詞在任一列都找不到。
    直接放棄的話就退回內建規則，標色計畫等於白做（實測就是這樣：
    該標紅的「欠了六七百萬」變成內建規則挑的「六七百萬」上金色）。

    所以找不到整詞時，退而求其次找**跨行的那一半**：
    這一列的結尾是不是詞的開頭，或這一列的開頭是不是詞的結尾。
    """
    i = row.find(word)
    if i >= 0:
        return i, i + len(word)
    # 這一列的結尾 = 詞的前半
    for n in range(len(word) - 1, _MIN_PARTIAL - 1, -1):
        if row.endswith(word[:n]):
            return len(row) - n, len(row)
    # 這一列的開頭 = 詞的後半
    for n in range(len(word) - 1, _MIN_PARTIAL - 1, -1):
        if row.startswith(word[-n:]):
            return 0, n
    return None


def decorate(text: str, extra: Optional[List[str]] = None,
             long_form: bool = False, plan: Optional[list] = None) -> str:
    """一站式：找關鍵詞並上色。

    `plan` 是判斷腦逐句挑好的 `[(詞, 語意色)]`（Pro）。有就照它上色，
    沒有就退回內建的正規表示式（免費版）——後者只抓得到數字與轉折詞，
    抓不到「AI ERP」「AI Native」這種要讀懂內容才知道該亮的。

    `long_form=True` 時密度收斂到 1/5——長片上滿色會變成整片閃爍。
    """
    if plan:
        spans = []
        for word, tone in plan:
            hit = _locate(text, word)
            # 位置一律自己找，不信判斷腦報的索引——跟「絕不信它回傳的秒數」
            # 是同一條紀律。找不到就跳過，硬套會上錯位置。
            if hit and not any(not (hit[1] <= s or hit[0] >= e)
                               for s, e, *_ in spans):
                spans.append((hit[0], hit[1], tone))
        # 有計畫就照計畫，這一列沒命中就**不上色**——不要退回內建規則。
        # 退回的話同一句會出現兩種上色（實測「一人公司10個／月就破千萬」
        # 第一列被內建規則挑了「一人」上金色，跟計畫挑的「破千萬」打架）。
        return colorize(text, sorted(spans)[:1 if long_form else 2])

    spans = find(text, extra, max_per_line=1 if long_form else 2)
    if long_form and spans:
        # 長片：只保留最高優先序的那一個
        spans = spans[:1]
    return colorize(text, spans)


def pick(segments: List[dict], llm, progress_cb=None) -> dict:
    """判斷腦逐句挑要亮的詞與語氣。回 `{段號: [[詞, 語意色], ...]}`。

    判準住在 Pro 規則包。免費版沒有這份 prompt，回空字典，
    上色就退回內建的正規表示式——抓得到數字，抓不到語意。

    ⚠️ 只回「詞」，不回位置。位置一律由引擎在該列字幕裡自己找——
    判斷腦報的索引不能信，這跟「絕不信它回傳的秒數」是同一條紀律。
    """
    from .. import rules as _rules
    from ..llm import FAST, LLMUnavailable

    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not segments:
        return {}
    pack = _rules.load()
    if not pack.has_prompt("keyword_color"):
        return {}

    numbered = "\n".join(f"{i+1}. {s.get('text','')}"
                         for i, s in enumerate(segments))
    # 免費版的判準只用單一顏色、Pro 的分三種語氣色，兩份 prompt 需要的
    # 變數不一樣。全部都餵進去，各自取用得到的——這樣同一份程式碼
    # 可以跑兩種判準，換規則包不用改引擎。
    prompt = pack.prompt("keyword_color", count=len(segments),
                         numbered=numbered, tones="、".join(TONE),
                         tones_default=DEFAULT_TONE)
    say(57, "判斷腦挑字幕要亮的關鍵詞…")
    try:
        data = llm.complete_json(prompt, tier=FAST)
    except LLMUnavailable:
        return {}

    out = {}
    for item in (data.get("marks") or []):
        if not isinstance(item, dict):
            continue
        try:
            i = int(item["seg"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= i < len(segments)):
            continue
        text = segments[i].get("text", "")
        picked = []
        for w in (item.get("words") or [])[:2]:
            if not isinstance(w, dict):
                continue
            word = str(w.get("word", "")).strip()
            tone = str(w.get("tone", DEFAULT_TONE)).strip()
            # 詞一定要真的出現在那一句裡，否則整條丟掉
            if not word or word not in text or len(word) > 8:
                continue
            picked.append([word, tone if tone in TONE else DEFAULT_TONE])
        if picked:
            out[i] = picked
    say(58, f"標色完成：{len(out)} 句")
    return out
