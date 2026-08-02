# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""畫面 QA —— 在**接縫處**抽幀，讓人或 AI 真的看到剪成什麼樣子。

## 為什麼要自己寫，不用通用抽幀工具

通用工具抽的是「場景關鍵幀」——畫面變化大的地方。但我們要驗的是**剪得對不對**，
關心的是每一個刀口前後那幾幀：接起來會不會跳、人物姿勢有沒有瞬移、
嘴型有沒有對不上。

那些位置通常畫面變化很小（同一個人繼續講話），場景偵測根本不會挑到它們。
所以抽幀時機必須自己控制。

## 產出

每個接縫產一張「前後並排」的對照圖，加一份 `_畫面QA/index.txt` 說明每張圖是哪一刀。
人可以直接翻，AI Agent 也可以逐張讀。
"""

import os
from typing import Callable, List, Optional

from . import media

# 接縫前後各取這麼多秒的畫面。0.12 秒約 3-4 幀，足以看出接得順不順。
OFFSET = 0.12


def _grab(video: str, t: float, out: str, width: int = 480) -> bool:
    """抽單張幀。"""
    r = media.ffmpeg(["-ss", f"{max(0.0, t):.3f}", "-i", video, "-frames:v", "1",
                      "-vf", f"scale={width}:-2", "-q:v", "3", "-y", out])
    return r.returncode == 0 and os.path.exists(out)


def seam_times(keep: List[dict]) -> List[float]:
    """算出成片裡每個接縫的時間點。

    保留區間在成片裡是首尾相接的，所以第 n 個接縫的位置＝前 n 段的總長度。
    """
    out, acc = [], 0.0
    for k in keep[:-1]:
        acc += k["end"] - k["start"]
        out.append(round(acc, 3))
    return out


def extract(video: str, keep: List[dict], out_dir: str,
            max_seams: int = 24,
            progress_cb: Optional[Callable] = None) -> dict:
    """在成片的每個接縫前後抽幀。回 `{count, dir, frames, note}`。

    接縫很多時只抽前 max_seams 個——一支片幾十個接縫，全抽會產出上百張圖，
    反而沒人看。**被略過的數量會明講**，不做無聲截斷。
    """
    def report(p, msg):
        if progress_cb:
            progress_cb(p, msg)

    if not os.path.exists(video):
        return {"count": 0, "dir": out_dir, "frames": [], "note": "找不到成片"}

    seams = seam_times(keep)
    if not seams:
        return {"count": 0, "dir": out_dir, "frames": [], "note": "只有一段，沒有接縫"}

    skipped = max(0, len(seams) - max_seams)
    seams = seams[:max_seams]
    os.makedirs(out_dir, exist_ok=True)

    report(98, f"畫面 QA：在 {len(seams)} 個接縫抽幀…")
    frames = []
    for i, t in enumerate(seams, 1):
        before = os.path.join(out_dir, f"{i:02d}_{t:.2f}s_前.jpg")
        after = os.path.join(out_dir, f"{i:02d}_{t:.2f}s_後.jpg")
        ok_b = _grab(video, t - OFFSET, before)
        ok_a = _grab(video, t + OFFSET, after)
        if ok_b and ok_a:
            frames.append({"seam": i, "time": t, "before": before, "after": after})

    # 索引檔：讓人與 AI 都知道每張圖對應哪一刀
    idx = os.path.join(out_dir, "index.txt")
    with open(idx, "w", encoding="utf-8") as f:
        f.write("接縫畫面對照\n" + "=" * 40 + "\n")
        f.write(f"成片：{os.path.basename(video)}\n")
        f.write(f"接縫數：{len(seam_times(keep))}"
                + (f"（只抽前 {max_seams} 個）" if skipped else "") + "\n\n")
        f.write("每個接縫抽前後各一張，看接起來順不順、人物有沒有瞬移。\n\n")
        for fr in frames:
            f.write(f"接縫 {fr['seam']:2d}　成片 {fr['time']:7.2f}s\n"
                    f"    前：{os.path.basename(fr['before'])}\n"
                    f"    後：{os.path.basename(fr['after'])}\n")

    note = f"抽了 {len(frames)} 組" + (f"，另有 {skipped} 個接縫未抽" if skipped else "")
    report(98, f"畫面 QA：{note}　→ {out_dir}")
    return {"count": len(frames), "dir": out_dir, "frames": frames, "note": note}
