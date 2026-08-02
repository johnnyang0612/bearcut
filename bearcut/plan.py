# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""下刀計畫：把各層的刀合併 → 算出保留區間 → 產出可複核的清單。

各偵測層各自產「要剪的區間」，彼此可能重疊。這裡負責統一收斂，
並產出一份**人看得懂**的對照表——使用者要能知道「剪了哪些、為什麼」，
而不是拿到一支黑箱剪好的片。
"""

import json
import os
from typing import List, Optional

from .rules import load as load_rules

TYPE_LABEL = {
    "silence": "靜音", "gap": "卡頓", "restart": "重講",
    "repeat": "重複/口吃", "junk": "廢段", "prep": "頭尾廢話",
    "mixed": "混合",
}


def reason_of(c: dict) -> str:
    label = TYPE_LABEL.get(c.get("type"), c.get("type", ""))
    return f"[{label}] {c.get('reason', '')}".strip()


def merge_cuts(cuts: List[dict], duration: float,
               gap_merge_sec: float = 0.1) -> List[dict]:
    """合併重疊或太靠近的刀。

    靠得很近（< gap_merge_sec）的兩刀一起併掉，免得中間留一個短到會閃的碎片。
    合併後保留**所有**理由（`reasons` 陣列），這樣對照表才能說明白
    「這一刀是靜音加重講一起造成的」。
    """
    if not cuts:
        return []

    clean = []
    for c in cuts:
        s = max(0.0, float(c["start"]))
        e = min(duration, float(c["end"]))
        if e > s:
            clean.append({**c, "start": s, "end": e})
    if not clean:
        return []
    clean.sort(key=lambda x: x["start"])

    merged = [dict(clean[0])]
    merged[0]["reasons"] = [reason_of(clean[0])]
    for c in clean[1:]:
        last = merged[-1]
        if c["start"] <= last["end"] + gap_merge_sec:
            last["end"] = max(last["end"], c["end"])
            last["reasons"].append(reason_of(c))
            if c.get("type") != last.get("type"):
                last["type"] = "mixed"
        else:
            nc = dict(c)
            nc["reasons"] = [reason_of(c)]
            merged.append(nc)

    for m in merged:
        m["start"] = round(m["start"], 3)
        m["end"] = round(m["end"], 3)
    return merged


def compute_keep(cuts: List[dict], duration: float,
                 min_keep_sec: Optional[float] = None) -> List[dict]:
    """算出要保留的區間（剪掉 cuts 之後剩下的）。

    太短（< min_keep_sec）的保留碎片直接捨棄——成片出現 0.x 秒的閃段比多剪一點更糟。
    """
    if min_keep_sec is None:
        min_keep_sec = load_rules().get("cut.min_keep_sec", 0.2)

    keep, pos = [], 0.0
    for c in cuts:
        if c["start"] > pos:
            keep.append({"start": pos, "end": c["start"]})
        pos = max(pos, c["end"])
    if pos < duration:
        keep.append({"start": pos, "end": duration})

    return [{"start": round(k["start"], 3), "end": round(k["end"], 3)}
            for k in keep if k["end"] - k["start"] >= min_keep_sec]


def _count_by_type(raw_cuts: List[dict], t: str) -> int:
    return sum(1 for c in raw_cuts if c.get("type") == t)


def build_plan(video: str, duration: float, raw_cuts: List[dict],
               merged: List[dict], keep: List[dict]) -> dict:
    """組出計畫物件。

    `raw_cuts` 是合併前的原始刀（用來統計各層貢獻），`merged` 是合併後實際下的刀。
    兩者都要——統計看的是「各層抓到多少」，下刀看的是「最後剪了什麼」。
    """
    kept_sec = sum(k["end"] - k["start"] for k in keep)
    cut_sec = max(0.0, duration - kept_sec)
    return {
        "video": video,
        "duration_sec": round(duration, 3),
        "summary": {
            "原長秒": round(duration, 3),
            "剪後秒": round(kept_sec, 3),
            "剪掉秒": round(cut_sec, 3),
            "剪掉比例": f"{(cut_sec / duration * 100) if duration else 0:.1f}%",
            "靜音段數": _count_by_type(raw_cuts, "silence"),
            "卡頓段數": _count_by_type(raw_cuts, "gap"),
            "重講段數": _count_by_type(raw_cuts, "restart"),
            "重複段數": _count_by_type(raw_cuts, "repeat"),
            "廢段數": _count_by_type(raw_cuts, "junk"),
            "保留片段數": len(keep),
        },
        "cuts": merged,
        "keep": keep,
    }


def write_plan_json(plan: dict, path: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    return path


def write_review_txt(plan: dict, path: str, note: str = "") -> str:
    """人看的「剪了哪些、為什麼」對照表。

    這份檔案是 BearCut 與黑箱工具最大的差別：使用者可以逐刀複核，
    不同意的話改 `_待剪清單.json` 再用 `--apply` 重剪。
    """
    s = plan["summary"]
    lines = [
        "剪輯判斷對照表",
        "=" * 60,
        f"影片　　：{os.path.basename(plan['video'])}",
        f"原長　　：{s['原長秒']} 秒",
        f"剪後　　：{s['剪後秒']} 秒（剪掉 {s['剪掉秒']} 秒，{s['剪掉比例']}）",
        f"保留片段：{s['保留片段數']} 段",
        "",
        f"各層抓到：靜音 {s['靜音段數']}　卡頓 {s['卡頓段數']}　"
        f"重講 {s['重講段數']}　重複 {s['重複段數']}　廢段 {s['廢段數']}",
        "=" * 60,
        "",
        "逐刀明細（不同意的話，改 _待剪清單.json 後用 --apply 重剪）",
        "-" * 60,
    ]
    for i, c in enumerate(plan["cuts"], 1):
        dur = c["end"] - c["start"]
        lines.append(f"{i:3d}. {c['start']:8.3f} → {c['end']:8.3f}　（{dur:.2f} 秒）")
        for r in c.get("reasons", []) or [reason_of(c)]:
            lines.append(f"      {r}")
        lines.append("")

    if note:
        lines += ["-" * 60, note, ""]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path
