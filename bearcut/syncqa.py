# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""音畫字同步 QA —— 抓「隨手打開成片就會發現」的錯。

這類錯誤的共同特徵是：**我們自己不打開檔案看就不會知道，但使用者一定會發現。**
所以必須自動驗，不能靠人記得檢查。

檢查項目都很基本，但每一項都真的出過事：
- 成片長度與計畫對不上（剪接參數錯、或 ffmpeg 中途失敗但回了 0）
- 字幕時間超出影片長度（時間軸換算錯，通常整份都偏掉）
- 影片沒有音軌（濾鏈寫錯時會發生，畫面正常但沒聲音）
- 字幕與語音起點差太多（字幕從第 30 秒才開始，前面全空）
"""

import os
from typing import Callable, List, Optional

from . import media
from .srtlint import parse_srt


def _ts_sec(ts: str) -> float:
    try:
        h, m, rest = ts.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    except (ValueError, AttributeError):
        return -1.0


def check(video: str, srt: Optional[str] = None,
          expected_sec: Optional[float] = None,
          progress_cb: Optional[Callable] = None) -> List[str]:
    """檢查成片。回問題清單（空的代表沒問題）。"""
    def report(p, msg):
        if progress_cb:
            progress_cb(p, msg)

    problems = []

    if not os.path.exists(video):
        return [f"找不到成片：{video}"]

    try:
        dur = media.get_duration(video)
    except Exception as e:
        return [f"讀不到成片長度（檔案可能損毀）：{e}"]

    if dur <= 0.1:
        problems.append("成片長度接近 0，剪接可能失敗了。")

    # 1. 長度與計畫是否吻合
    if expected_sec and expected_sec > 0:
        drift = abs(dur - expected_sec)
        if drift > max(1.0, expected_sec * 0.02):
            problems.append(
                f"成片長度 {dur:.1f}s 與計畫的 {expected_sec:.1f}s 差了 {drift:.1f}s，"
                "剪接可能有問題。")

    # 2. 有沒有音軌
    out = media.ffprobe(["-select_streams", "a", "-show_entries",
                         "stream=codec_type", "-of", "csv=p=0", video])
    if "audio" not in (out.stdout or ""):
        problems.append("成片沒有音軌——畫面正常但會沒有聲音。")

    # 3. 字幕時間軸
    if srt and os.path.exists(srt):
        cues = parse_srt(srt)
        if not cues:
            problems.append("字幕檔讀不到內容。")
        else:
            last_end = max(_ts_sec(c["end"]) for c in cues)
            first_start = min(_ts_sec(c["start"]) for c in cues)
            if last_end > dur + 1.0:
                problems.append(
                    f"字幕最後一句在 {last_end:.1f}s，但成片只有 {dur:.1f}s——"
                    "時間軸換算錯了，整份字幕可能都偏掉。")
            if first_start > min(30.0, dur * 0.25):
                problems.append(
                    f"字幕從 {first_start:.1f}s 才開始，前面一大段沒有字幕，"
                    "可能有段落被漏掉。")

    if progress_cb:
        if problems:
            report(98, f"成片檢查發現 {len(problems)} 項問題：")
            for p_ in problems:
                report(98, f"    · {p_}")
        else:
            report(98, "成片檢查：音畫字都正常")
    return problems
