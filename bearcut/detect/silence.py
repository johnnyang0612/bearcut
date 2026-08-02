# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""靜音偵測 —— 用 ffmpeg silencedetect 抓沒有聲音的空白。

**最可靠的一層。** 靠聲音能量判斷，比用 whisper 字間空檔推算準得多，
而且零成本、不需要判斷腦。沒有 LLM 的使用者至少還有這一層可用。
"""

import re
from typing import List, Optional

from .. import media
from ..rules import load as load_rules


def detect_silences(input_path: str, noise_db: float = -30,
                    min_silence_sec: float = 0.6) -> List[dict]:
    """回偵測到的靜音區間 `[{"start", "end"}]`（秒）。

    noise_db：低於這個分貝視為靜音（-30 ~ -35 常用，越接近 0 越嚴格）。
    min_silence_sec：至少這麼長才算一段。
    """
    out = media.ffmpeg([
        "-i", input_path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
        "-f", "null", "-",
    ])
    # silencedetect 的結果印在 stderr，不是 stdout
    stderr = out.stderr or ""

    silences: List[dict] = []
    cur_start: Optional[float] = None
    for line in stderr.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            cur_start = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if m and cur_start is not None:
            end = float(m.group(1))
            start = max(0.0, cur_start)
            if end > start:
                silences.append({"start": start, "end": end})
            cur_start = None
    return silences


def silence_cuts(input_path: str, duration: float, cfg: Optional[dict] = None) -> List[dict]:
    """把靜音轉成「要剪掉的區間」。

    **不是把靜音砍到 0。** 每段保留一點自然停頓，只剪掉多出來的部分——
    全部剪光會讓成片急促到不像人在講話。兩端再各留一點緩衝，避免切到尾音或起音。

    回 `[{"start", "end", "type": "silence", "reason"}]`。
    """
    c = cfg or load_rules().section("silence")
    noise_db = c.get("noise_db", -30)
    min_silence = c.get("min_silence_sec", 0.45)
    keep = c.get("keep_silence_sec", 0.3)
    pad = c.get("edge_pad_sec", 0.05)

    cuts = []
    for s in detect_silences(input_path, noise_db=noise_db, min_silence_sec=min_silence):
        dur = s["end"] - s["start"]
        reserve = keep + pad * 2          # 想保留的總停頓（含兩端緩衝）
        if dur <= reserve:
            continue                      # 這段不夠長，不值得剪

        cut_start = max(0.0, s["start"] + pad + keep / 2)
        cut_end = min(duration, s["end"] - pad - keep / 2)
        if cut_end - cut_start <= 0.05:
            continue

        cuts.append({
            "start": round(cut_start, 3),
            "end": round(cut_end, 3),
            "type": "silence",
            "reason": f"靜音 {dur:.1f} 秒（保留 {keep:.1f} 秒停頓）",
        })
    return cuts
