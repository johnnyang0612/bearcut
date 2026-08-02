# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""剪前自我修正 —— **在真的剪下去之前**先檢查會剪成什麼樣子。

## 為什麼是剪之前

剪完才發現接縫不通順，就得重新編碼一次（很貴）。這一層在下刀前先「模擬」
剪完的逐字稿，把接縫攤開給判斷腦看，有問題就改刀，改到乾淨才真的動手。

## 雙向修正：不只補漏剪，也還原多剪

多數工具只會「再多剪一點」，但**剪過頭比漏剪更難救**——漏剪看得出來，
多剪則是內容默默消失，使用者要看片才發現少了一句話。

所以這裡兩個方向都做：
- **cut**：接縫殘留 → 延伸既有的刀，或補一刀
- **restore**：該留的被剪掉 → 從重疊的刀裡「相減」把那段露出來

`restore` 用相減而不是「整段必須在同一把刀內」，因為被剪掉的內容常常是
一半保留、一半被刀吃掉，相減才能精準只露出真正少掉的那截。

## 安全機制

判斷腦只回「文字錨點」（前後各幾個字），**位置一律由我們自己在字流裡比對定位**。
對不上就跳過那一筆——寧可不修，也不要亂改。並且限制單次最多改幾筆。
"""

import json
from typing import Callable, List, Optional, Tuple

from . import rules as _rules
from .llm import FAST, LLMError, Provider

SEAM = "｜"
MAX_ROUNDS = 3
MAX_EDITS = 12


def _in_cut(t: float, cuts: List[dict]) -> bool:
    return any(c["start"] <= t <= c["end"] for c in cuts)


def simulate(chars: List[dict], cuts: List[dict]) -> Tuple[List[dict], set]:
    """模擬剪完之後留下哪些字，並標出接縫位置。

    回 `(留下的字, 接縫索引集合)`。接縫索引指的是「這個字之前有一刀」。
    """
    kept, seams = [], set()
    was_cut = False
    for ch in chars:
        mid = (ch["start"] + ch["end"]) / 2
        if _in_cut(mid, cuts):
            was_cut = True
            continue
        if was_cut and kept:
            seams.add(len(kept))
        was_cut = False
        kept.append(ch)
    return kept, seams


def render(kept: List[dict], seams: set) -> str:
    """把留下的字render成文字，接縫處插入標記。"""
    out = []
    for i, ch in enumerate(kept):
        if i in seams:
            out.append(SEAM)
        out.append(ch["char"])
    return "".join(out)


def _find_span(chars: List[dict], before: str, target: str,
               after: str) -> Optional[Tuple[float, float]]:
    """用前後文錨點在字流裡定位一段文字，回 (start, end)。

    對不上回 None——這是安全機制，寧可不修也不要改錯地方。
    """
    text = "".join(c["char"] for c in chars)
    target = (target or "").strip()
    if not target:
        return None

    # 優先用「前錨 + 目標」一起找，比單找目標精準得多（同樣的字可能出現很多次）
    for probe in (f"{before}{target}", target):
        if not probe:
            continue
        idx = text.find(probe)
        if idx < 0:
            continue
        start_i = idx + len(probe) - len(target)
        end_i = start_i + len(target) - 1
        if 0 <= start_i <= end_i < len(chars):
            return chars[start_i]["start"], chars[end_i]["end"]
    return None


def _subtract(cuts: List[dict], lo: float, hi: float) -> List[dict]:
    """把 [lo, hi] 從所有與它重疊的刀裡扣掉（縮刀或拆刀）。"""
    out = []
    for c in cuts:
        s, e = c["start"], c["end"]
        if hi <= s or lo >= e:
            out.append(c)
            continue
        if lo > s:
            out.append({**c, "end": round(lo, 3)})
        if hi < e:
            out.append({**c, "start": round(hi, 3)})
    return [c for c in out if c["end"] - c["start"] > 0.02]


def verify_and_repair(
    chars: List[dict],
    cuts: List[dict],
    llm: Provider,
    script_text: Optional[str] = None,
    max_rounds: int = MAX_ROUNDS,
    progress_cb: Optional[Callable] = None,
    debug_path: Optional[str] = None,
) -> List[dict]:
    """迴圈修正到接縫乾淨。回修正後的刀。"""
    def report(p, msg):
        if progress_cb:
            progress_cb(p, msg)

    if not chars or not cuts or llm is None or not llm.available():
        return cuts

    work = [dict(c) for c in cuts]
    edits = 0
    dbg = {"rounds": []}

    for rnd in range(1, max_rounds + 1):
        kept, seams = simulate(chars, work)
        if not kept:
            break
        kept_text = render(kept, seams)
        report(94, f"剪前檢查第 {rnd} 輪：審核 {len(kept)} 字、{len(seams)} 個接縫…")

        script_hint = ("下面附了拍攝腳本，可以對照著看哪裡少了。"
                       if script_text else
                       "沒有腳本可對照，所以只挑「讀起來明顯有斷層」的地方。")
        prompt = _rules.load().prompt(
            "verify_seams", kept_text=kept_text, script_hint=script_hint,
            script_section=(f"\n## 拍攝腳本\n\n{script_text}\n" if script_text else ""))

        try:
            review = llm.complete_json(prompt, tier=FAST)
        except LLMError as e:
            report(94, f"剪前檢查：審核失敗，保持原樣不修（不冒誤剪風險）：{e}")
            break
        if not isinstance(review, dict):
            break

        to_cut = review.get("cut") or []
        to_restore = review.get("restore") or []
        dbg["rounds"].append({"kept_text": kept_text, "review": review})

        if not to_cut and not to_restore:
            report(94, f"剪前檢查第 {rnd} 輪：接縫乾淨，通過")
            break

        changed = False

        # ── restore 先做：把剪過頭的補回來 ────────────────────────────
        # 先做的理由：補回來之後，接下來要剪的殘留才不會又把剛補的吃掉。
        restored = []
        for it in to_restore:
            if edits >= MAX_EDITS:
                break
            if not isinstance(it, dict):
                continue
            span = _find_span(chars, it.get("before", ""), it.get("missing", ""),
                              it.get("after", ""))
            if not span:
                report(94, f"  補回「{it.get('missing')}」定位不到，略過")
                continue
            lo, hi = span
            if not (0.05 <= hi - lo <= 4.0):
                continue                    # 太短或太長都不安全
            work = _subtract(work, lo, hi)
            restored.append((lo, hi))
            edits += 1
            changed = True
            report(94, f"  補回被剪掉的「{it.get('missing')}」")

        # ── cut：補漏剪的殘留 ─────────────────────────────────────────
        for it in to_cut:
            if edits >= MAX_EDITS:
                break
            if not isinstance(it, dict):
                continue
            span = _find_span(chars, it.get("before", ""), it.get("text", ""),
                              it.get("after", ""))
            if not span:
                continue
            lo, hi = span
            if not (0.02 <= hi - lo <= 3.0):
                continue
            # 避開這輪剛補回來的區間——否則會把剛救回來的內容又剪掉
            if any(not (hi <= rs or lo >= re_) for rs, re_ in restored):
                continue
            work.append({"start": round(lo, 3), "end": round(hi, 3),
                         "type": "repeat",
                         "reason": f"接縫殘留「{it.get('text')}」"
                                   + (f"（{it.get('reason')}）" if it.get("reason") else "")})
            edits += 1
            changed = True
            report(94, f"  補剪接縫殘留「{it.get('text')}」")

        if not changed:
            report(94, f"剪前檢查第 {rnd} 輪：提議都定位不到，停止")
            break

    if debug_path:
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                json.dump(dbg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    if edits:
        report(94, f"剪前檢查完成：共修正 {edits} 處")
    return sorted(work, key=lambda c: c["start"])
