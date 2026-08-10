# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""校字 —— **必須在判斷要剪哪裡之前跑**。

## 為什麼順序不能反

whisper 會把「一人公司」聽成「藝人公司」、「全額退費」聽成「全能做到」。
如果拿沒校正的文字去判斷要剪哪裡，判斷腦會看到一堆讀不通的句子，
把**講者其實講得很好的段落**當成廢段刪掉。

實測：R09 少了 4 段重複判斷，就是因為沒有先校字。

## 三道防線確保校字不會變成改寫

校字只能改「字」，不能動結構。所以：

1. **段數必須完全相同** —— 少一段或多一段就整批放棄，用原文
2. **單段長度差超過 30% 就退回原文** —— 那是在改寫不是修錯字
3. **時間戳完全不動** —— 只換 text 欄位

## 兩層修正

- **replacements 查表**：每次都會聽錯的固定詞（品牌名、行話），直接換，
  100% 可靠又不花 token
- **LLM 校字**：沒見過的、需要上下文判斷的
"""

import json
import re
from typing import Callable, List, Optional, Tuple

from . import rules as _rules
from .llm import FAST, STRONG, LLMError, Provider
from .rules import RULEPACK_DIR

MAX_LEN_DRIFT = 0.30      # 單段長度差超過此比例就不採用（見上方防線 2）


def _load_lexicon() -> Tuple[dict, list]:
    """讀進階規則包的校字詞表 `lexicon/corrections.json`。沒有就回空的。

    ## 為什麼是獨立一個檔，不是直接蓋掉 replacements.json

    底包的 `replacements.json` 出廠是空的，並且明講「使用者可以自由增修」——
    那是**使用者的檔**。進階包若把自己的表寫進同一個路徑，`update._merge_into`
    會直接覆蓋，使用者手改的品牌詞就在裝進階包的當下無聲消失。

    所以進階包放自己的 `lexicon/`，兩層在這裡合併。優先序（後者蓋前者）：
    底包 → 進階包 → 使用者記憶。

    格式跟底包不同：進階包的表依來源分組（typo / cn_to_tw…），每組各有
    `$desc` 與 `map`，這樣客戶端 UI 才說得出「這條為什麼被改」。這裡只把所有
    `map` 攤平成一張替換表；`$` 開頭的欄位一律是註解，不是資料。
    """
    repl: dict = {}
    hot: list = []
    p = RULEPACK_DIR / "lexicon" / "corrections.json"
    if not p.exists():
        return repl, hot
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return repl, hot                          # 詞表壞掉不該讓整條校字失敗
    for group, body in (d.get("replacements") or {}).items():
        if group.startswith("$") or not isinstance(body, dict):
            continue
        for k, v in (body.get("map") or {}).items():
            if not k.startswith("$") and isinstance(v, str):
                repl[k] = v
    hot = [w for w in (d.get("hotwords") or []) if isinstance(w, str)]
    return repl, hot


def load_replacements() -> Tuple[dict, list]:
    """讀替換表與詞彙表。回 `(replacements, hotwords)`。

    三個來源合併，後者蓋前者：底包規則（通用）→ 進階包 `lexicon/`（見
    `_load_lexicon`）→ 使用者的校正記憶（`bearcut learn` 學來的）。
    使用者的最優先——他親手修過的東西，比通用規則更知道自己在講什麼。
    """
    repl, hot = {}, []
    p = RULEPACK_DIR / "replacements.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            repl = {k: v for k, v in (d.get("replacements") or {}).items()
                    if not k.startswith("$")}
            hot = list(d.get("hotwords") or [])
        except json.JSONDecodeError:
            pass
    x_repl, x_hot = _load_lexicon()
    repl.update(x_repl)
    hot += [w for w in x_hot if w not in hot]
    try:
        from .learn import load as _load_memory
        u_repl, u_hot = _load_memory()
        repl.update(u_repl)                       # 使用者的蓋過規則包的
        hot += [w for w in u_hot if w not in hot]
    except Exception:
        pass                                      # 沒有記憶檔不該讓校字失敗
    return repl, hot


def apply_replacements(segments: List[dict], repl: dict,
                       progress_cb: Optional[Callable] = None) -> List[dict]:
    """套用固定替換表。

    whisper 偶爾整片吐簡體，只放繁體 key 會完全對不上 →
    自動把每條 key 的簡體變體也補進表（繁體原 key 優先）。
    """
    if not repl:
        return segments
    try:
        from zhconv import convert as zhc
        repl = {**{zhc(k, "zh-cn"): v for k, v in repl.items()}, **repl}
    except ImportError:
        pass

    n = 0
    out = []
    for seg in segments:
        t = seg["text"]
        for a, b in repl.items():
            if a and a in t:
                t = t.replace(a, b)
        if t != seg["text"]:
            n += 1
        out.append({**seg, "text": t})
    if n and progress_cb:
        progress_cb(88, f"固定替換表修正了 {n} 段")
    return out


def fix_typos(segments: List[dict], llm: Provider,
              script_text: Optional[str] = None,
              hotwords: Optional[list] = None,
              progress_cb: Optional[Callable] = None) -> Tuple[List[dict], List[str]]:
    """用判斷腦校錯字。回 `(校正後的 segments, 疑義清單)`。

    任何異常都回原文——校字失敗不該讓整支片跑不完，頂多是判斷品質差一點。
    """
    def report(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not segments or llm is None or not llm.available():
        return segments, []

    lines = [s["text"] for s in segments]
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(lines))

    hotword_rule = ""
    if hotwords:
        hotword_rule = ("\n   本片可能出現的專有名詞（辨識聽錯時請修成這些寫法）："
                        + "、".join(hotwords[:80]))
    script_section = ""
    if script_text:
        script_section = f"\n## 拍攝腳本（專有名詞以此為準）\n\n{script_text}\n"

    prompt = _rules.load().prompt(
        "fix_typos", count=len(lines), numbered=numbered,
        script_section=script_section, hotword_rule=hotword_rule)

    report(89, "判斷腦校正錯字中（先校正、再決定哪裡要剪）…")
    try:
        data = llm.complete_json(prompt, tier=FAST)
    except LLMError as e:
        report(89, f"⚠ 校字失敗，改用辨識原文繼續：{e}")
        return segments, []

    fixed = data.get("lines") if isinstance(data, dict) else data
    suspects = []
    if isinstance(data, dict):
        suspects = [s for s in (data.get("suspects") or [])
                    if isinstance(s, str) and s.strip()]

    # 防線 1：段數必須完全相同
    if not isinstance(fixed, list) or len(fixed) != len(lines):
        report(89, f"⚠ 校字回傳段數不符（預期 {len(lines)}，"
                   f"得到 {len(fixed) if isinstance(fixed, list) else '非陣列'}），"
                   "放棄校字、改用原文")
        return segments, []

    # 防線 2：單段長度差太多代表在改寫，該段不採用
    out, changed, rejected = [], 0, 0
    for seg, new in zip(segments, fixed):
        keep = seg["text"]
        if isinstance(new, str) and new.strip():
            new = new.strip()
            if abs(len(new) - len(keep)) <= max(2, len(keep) * MAX_LEN_DRIFT):
                if new != keep:
                    changed += 1
                keep = new
            else:
                rejected += 1
        # 防線 3：只換 text，時間戳原封不動
        out.append({**seg, "text": keep})

    msg = f"校字完成：修正 {changed} 段"
    if rejected:
        msg += f"（{rejected} 段疑似改寫，已退回原文）"
    report(89, msg)
    return out, suspects


def escalate_suspects(segments: List[dict], suspects: List[str], llm: Provider,
                      script_text: Optional[str] = None,
                      progress_cb: Optional[Callable] = None) -> Tuple[List[dict], List[str]]:
    """疑義二審：快模型標「拿不準」的，用強模型帶上下文再看一次。

    能修的直接修（一樣守段數與長度），真的無法辨識才留給人工。
    這一層是選用的——失敗就照舊把疑義留給人工，不影響主流程。
    """
    def report(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not suspects or not llm.available():
        return segments, suspects

    idx = set()
    for s in suspects:
        m = re.search(r"第\s*(\d+)\s*段", s)
        if m:
            i = int(m.group(1)) - 1
            if 0 <= i < len(segments):
                idx.add(i)
    if not idx:
        return segments, suspects

    # 帶前後各一段當上下文——很多錯字只有看到鄰句才判得出來
    ctx = sorted({j for i in idx for j in (i - 1, i, i + 1)
                  if 0 <= j < len(segments)})
    numbered = "\n".join(f"{j + 1}. {segments[j]['text']}" for j in ctx)
    listed = "\n".join(f"- {s}" for s in suspects[:30])

    prompt = (
        "你是資深字幕校對。下面是語音辨識的片段，以及初審標記為「拿不準」的地方。\n"
        "請帶上下文重看一次，能確定的就修正，真的無法判斷的才保留原樣。\n\n"
        "規則：只能修錯字，**字數不可增減**，不可改寫、不可調整語氣。\n"
        "一律輸出台灣繁體中文。\n\n"
        f"## 疑義清單\n{listed}\n\n"
        f"## 片段（含前後文）\n{numbered}\n"
        + (f"\n## 拍攝腳本\n{script_text}\n" if script_text else "")
        + '\n只輸出 JSON：{"fixes": [{"index": 段號, "text": "修正後全文"}], '
          '"unresolved": ["仍無法判斷的提醒"]}\n'
    )

    report(89, f"疑義二審中（{len(idx)} 處，使用較強模型）…")
    try:
        data = llm.complete_json(prompt, tier=STRONG)
    except LLMError as e:
        report(89, f"疑義二審略過，提醒照舊留給人工：{e}")
        return segments, suspects

    out = list(segments)
    fixed_n = 0
    for f in (data.get("fixes") or []):
        try:
            i = int(f["index"]) - 1
            new = str(f["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= i < len(out)) or not new:
            continue
        old = out[i]["text"]
        # 二審同樣守長度，不讓它藉機改寫
        if abs(len(new) - len(old)) <= max(2, len(old) * MAX_LEN_DRIFT):
            out[i] = {**out[i], "text": new}
            fixed_n += 1

    unresolved = [s for s in (data.get("unresolved") or []) if isinstance(s, str)]
    if fixed_n:
        report(89, f"疑義二審修正 {fixed_n} 處，{len(unresolved)} 處仍需人工確認")
    return out, unresolved or []
