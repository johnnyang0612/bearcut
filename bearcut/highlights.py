# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""自動挑精華：從一支長片裡找出可以獨立成立的段落，各自做成直式短影音。

## 職責邊界（這支檔案存在的理由）

**判斷腦只決定「挑哪幾段」，秒數一律由我們自己的字幕反查。**
LLM 回的是**段號**，不是時間——`_resolve()` 拿段號去 `segments` 查起訖秒。
這是全系統的鐵則（見 CLAUDE.md 設計原則 1），在這裡尤其要緊：
挑精華讀的是整支長片的逐字稿，模型報的秒數偏移量會非常大，
信了就會剪到不相干的地方，而且錯得很安靜。

**判準不在這支檔案裡。** 「什麼段落值得剪成短片」「hook 怎麼挑」「結尾 CTA
怎麼寫」全部住在規則包的 `prompts/highlights.md`。這裡只負責：組題目、
呼叫判斷腦、把回來的段號翻成時間、切片、交給 `shortform` 渲染。

免費規則包沒有那份 prompt，所以免費使用者跑這個指令會得到一句白話的
「需要 Pro 規則包」——不是壞掉，是這個功能的判準要訂閱才有。
機構開源、判準付費，邊界是天然的。

## 一條精華片長什麼樣

    [ hook 3~5 秒 ]  [ ────────── 本體 ────────── ]
      冷開場鉤子        可以獨立聽懂的完整段落

hook 是從本體**裡面**挑出來的最有張力那一句，複製到最前面當預告。
所以同一段聲音會出現兩次——這是刻意的，不是 bug。
`cut_video` 吃保留區間清單，把 hook 與本體當成兩段保留區間一次剪完，
不需要另外接合。
"""

import json
import os
import re
from typing import Callable, Dict, List, Optional

from . import rules as _rules
from .llm import FAST, LLMUnavailable, get_llm
from .rules import RulepackError

# teaser 只是預告，太長就沒有預告的效果了
MAX_HOOK_SEC = 5.0
# 短到這個程度的段落不可能獨立成立，多半是模型把段號抄錯
MIN_BODY_SEC = 3.0


def _ts_to_sec(ts: str) -> float:
    hh, mm, rest = ts.strip().split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def parse_srt(srt_path: str) -> List[dict]:
    """讀 SRT 成 `[{start, end, text}]`。這是段號與時間的唯一事實來源。"""
    try:
        raw = open(srt_path, "r", encoding="utf-8").read().strip()
    except OSError as e:
        raise RuntimeError(f"讀不到字幕檔：{srt_path}\n{e}") from e

    segs = []
    for blk in re.split(r"\n\s*\n", raw):
        lines = [ln for ln in blk.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        tline = next((ln for ln in lines if "-->" in ln), None)
        if not tline:
            continue
        s, e = [t.strip() for t in tline.split("-->")]
        text = "".join(lines[lines.index(tline) + 1:]).strip()
        if not text:
            continue
        segs.append({"start": _ts_to_sec(s), "end": _ts_to_sec(e), "text": text})
    return segs


def _resolve(clip: dict, segs: List[dict]) -> Optional[dict]:
    """把判斷腦回的**段號**翻成時間。回 None 代表這條不能用。

    這裡是「不信 LLM 秒數」的落地點：clip 裡就算帶了秒數也一律忽略，
    只讀 from/to/hook_from/hook_to 這幾個段號，時間全部從 segs 反查。
    """
    n = len(segs)
    try:
        i0 = int(clip["from"]) - 1
        i1 = int(clip["to"]) - 1
        h0 = int(clip.get("hook_from", clip["from"])) - 1
        h1 = int(clip.get("hook_to", clip.get("hook_from", clip["from"]))) - 1
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= i0 <= i1 < n):
        return None
    # hook 必須落在本體之內；落空就退回用本體第一段，不要整條丟掉
    if not (i0 <= h0 <= h1 <= i1):
        h0 = h1 = i0

    body_s, body_e = round(segs[i0]["start"], 3), round(segs[i1]["end"], 3)
    hook_s, hook_e = round(segs[h0]["start"], 3), round(segs[h1]["end"], 3)
    if hook_e - hook_s > MAX_HOOK_SEC:
        hook_e = round(hook_s + MAX_HOOK_SEC, 3)
    if body_e - body_s < MIN_BODY_SEC:
        return None

    try:
        score = float(clip.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0

    txt = lambda a, b: "".join(segs[k]["text"] for k in range(a, b + 1)).strip()
    return {
        "score": score,
        "type": str(clip.get("type", "")).strip(),
        "body_start": body_s, "body_end": body_e,
        "body_dur": round(body_e - body_s, 3),
        "hook_start": hook_s, "hook_end": hook_e,
        "hook_text": txt(h0, h1),
        "title": str(clip.get("title", "")).strip(),
        "promise": str(clip.get("promise", "")).strip(),
        "ending_hook": str(clip.get("ending_hook", "")).strip(),
        "reason": str(clip.get("reason", "")).strip(),
        "text": txt(i0, i1)[:80],
    }


def select(segments: List[dict], max_n: int = 6,
           min_sec: float = 18.0, max_sec: float = 75.0,
           progress_cb: Optional[Callable] = None) -> List[dict]:
    """讀整篇字幕，選出精華段落。回依分數排序的清單。

    判準來自規則包的 `prompts/highlights.md`；免費包沒有那份 prompt。
    """
    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not segments:
        return []

    numbered = "\n".join(f"{i+1}. {s['text']}" for i, s in enumerate(segments))
    pack = _rules.load()
    if not pack.has_prompt("highlights"):
        # 免費包沒有這份判準。講清楚這不是壞掉，也不要假裝在跑。
        raise RulepackError(
            "自動挑精華需要 Pro 規則包。\n"
            "免費版的順剪、字幕、直式短影音、長片接合都不受影響，照常可用。\n"
            "已經訂閱的話：bearcut login <你的授權碼> 之後再跑一次 bearcut update。")
    # 判準在，卻還是渲染失敗＝程式問題，不要誤導成授權問題，直接讓它炸上去
    prompt = pack.prompt(
        "highlights", count=len(segments), numbered=numbered,
        n_cand=max(max_n * 2, 12),
        min_sec=f"{min_sec:.0f}", max_sec=f"{max_sec:.0f}")

    llm = get_llm()
    say(20, f"判斷腦讀完整份字幕（{len(segments)} 段），挑精華中…")
    try:
        data = llm.complete_json(prompt, tier=FAST)
    except LLMUnavailable as e:
        raise RuntimeError(
            "沒有可用的判斷腦，挑精華需要它讀懂內容。\n"
            "請跑 bearcut doctor 看「判斷腦」那一列怎麼補。\n"
            f"（{e}）") from e

    clips = [c for c in (_resolve(x, segments)
                         for x in (data.get("clips") or []) if isinstance(x, dict))
             if c]
    # 同分時本體較長的優先——比較有機會獨立成立
    clips.sort(key=lambda c: (c["score"], c["body_dur"]), reverse=True)
    say(30, f"選出 {len(clips)} 條候選")
    return clips


def make(video: str, srt: Optional[str] = None, count: int = 3,
         output_dir: Optional[str] = None,
         min_sec: float = 18.0, max_sec: float = 75.0,
         plan_only: bool = False,
         progress_cb: Optional[Callable] = None) -> dict:
    """從一支長片產出 `count` 支直式精華短片。

    回 `{clips: [...], outputs: [...], plan: 路徑}`。
    """
    from . import cut as _cut
    from . import shortform as _sf

    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not os.path.isfile(video):
        raise FileNotFoundError(f"找不到影片：{video}")

    base = os.path.splitext(os.path.basename(video))[0].replace("_淨毛片", "")
    d = output_dir or os.path.dirname(os.path.abspath(video))
    os.makedirs(d, exist_ok=True)

    srt = srt or os.path.join(d, f"{base}_字幕.srt")
    if not os.path.isfile(srt):
        raise FileNotFoundError(
            f"找不到字幕檔：{srt}\n"
            "挑精華要先有字幕才知道每一段在講什麼。\n"
            f"請先跑 bearcut cut {os.path.basename(video)}，或用 --srt 指定字幕檔。")

    say(10, "讀字幕…")
    segs = parse_srt(srt)
    if not segs:
        raise RuntimeError(f"字幕檔是空的或格式不對：{srt}")

    clips = select(segs, max_n=count, min_sec=min_sec, max_sec=max_sec,
                   progress_cb=progress_cb)
    if not clips:
        say(100, "沒有選出夠好的段落")
        return {"clips": [], "outputs": [], "plan": None}

    # 整個候選池都存起來：之後想多剪幾條可以直接從池子續剪，
    # 不必重讀字幕、重問判斷腦。長片問一次很貴。
    plan_path = os.path.join(d, f"{base}_精華清單.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump({"video": video, "srt": srt, "clips": clips},
                  f, ensure_ascii=False, indent=2)
    say(35, f"精華清單已存：{os.path.basename(plan_path)}")

    if plan_only:
        return {"clips": clips, "outputs": [], "plan": plan_path}

    picked = clips[:count]
    outputs = []
    for i, c in enumerate(picked, 1):
        lo = 35 + int(60 * (i - 1) / len(picked))
        hi = 35 + int(60 * i / len(picked))
        tag = f"[{i}/{len(picked)}]"
        say(lo, f"{tag} 剪出精華：{c['title'] or c['text'][:20]}…")

        # hook 與本體當成兩段保留區間，cut_video 會接起來——同一段聲音
        # 在 hook 與本體各出現一次是刻意的（預告 + 正片）
        keep = [{"start": c["hook_start"], "end": c["hook_end"]},
                {"start": c["body_start"], "end": c["body_end"]}]
        raw = os.path.join(d, f"{base}_精華{i}_原始.mp4")
        _cut.cut_video(video, keep, raw,
                       progress_cb=lambda p, m: say(lo + (hi - lo) * p // 200, f"{tag} {m}"))

        say((lo + hi) // 2, f"{tag} 做成直式短影音…")
        res = _sf.make(raw, title=c["title"] or None, cta=c["ending_hook"] or None,
                       output_dir=d,
                       progress_cb=lambda p, m: say(lo + (hi - lo) * (100 + p) // 200,
                                                    f"{tag} {m}"))
        outputs.append({**c, "raw": raw, **res})

    say(100, f"完成 {len(outputs)} 支精華短片")
    return {"clips": clips, "outputs": outputs, "plan": plan_path}
