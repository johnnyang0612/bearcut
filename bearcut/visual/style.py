# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""視覺樣式與安全框 —— 所有燒錄的共同基礎。

## 安全框是實測值，不是估的

平台 UI 會蓋掉畫面的一部分，被蓋到的字卡等於沒做。這裡的數字是實際量出來的：

    頂部 UI            y < 115
    底部帳號與內文      y > 1575
    右側互動欄          x > 890，**且只從 y ≥ 1090 開始**

最後那個「只從 y≥1090 開始」很關鍵：上半部其實可以用到 960 寬，
把整個右側都當禁區會白白浪費版面。

## ASS 顏色是 &HBBGGRR&

BGR 反序，不是 RGB。這是寫 ASS 最常錯的地方。
"""

from typing import Dict, List, Optional

from ..rules import load as load_rules

# 直式畫布
W, H = 1080, 1920

# 實測安全框（見模組說明）
SAFE = {
    "top_ui_y": 115,
    "bottom_ui_y": 1575,
    "rail_x": 890,
    "rail_y": 1090,
    "center_max_w": 700,     # (890-540)*2 —— y≥1090 的置中元素上限
    "top_max_w": 960,
}

# 三帶版面，彼此不重疊
BANDS = {
    "top": (0, 390),         # 標題 / POV 大字卡
    "middle": (400, 1008),   # 影片（16:9 縮到寬 1080 = 高 608）
    "bottom": (1008, 1650),  # CTA + 字幕
}

# 已驗證可用的定位點，不要自己重算
ANCHORS = {
    "title_y": 220, "promise_y": 350,
    "card_top_y": 300, "card_key_y": 450,
    "cta_y": 1160, "sub_bottom_margin": 400,
}

# 與現行成片同一組色票——換色會讓新舊片看起來不像同一個頻道
PALETTE = {
    "white": "&H00FFFFFF", "black": "&H00000000",
    "yellow": "&H0000C8FF", "orange": "&H000A7DFF",
    "cyan": "&H00F0D060", "red": "&H004040FF",
    "green": "&H0060C878", "navy": "&H00502D1A",
}

TYPE = {
    "font": "Source Han Sans TC Heavy",
    "sub_size": 72, "sub_outline": 5.0, "sub_shadow": 1.4,
    "title_size": 90, "title_outline": 6.0,
    "cta_size": 82, "card_top_size": 84, "card_key_size": 108,
    "fade_ms": 150, "sub_max_len": 8,
}


def esc(s: str) -> str:
    """ASS 用的路徑跳脫（Windows 的反斜線與冒號會被當成語法）。"""
    return str(s).replace("\\", "/").replace(":", "\\:")


def clean_text(s: str) -> str:
    """把會破壞 ASS 語法的字元清掉。

    `{}` 是 ASS 的覆寫標籤語法，`\\` 是跳脫字元，直接燒進去會讓整行消失或亂掉。
    `【】` 是我們內部標記強調用的，不該出現在畫面上。
    """
    if not s:
        return ""
    for a, b in (("{", "("), ("}", ")"), ("\\", "／"),
                 ("【", ""), ("】", ""), ("\n", " ")):
        s = s.replace(a, b)
    return s.strip()


def ts(t: float) -> str:
    """秒 → ASS 時間碼 `H:MM:SS.cc`。"""
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}.{int(round((t - int(t)) * 100)):02d}"


def fit_size(text: str, base: int, max_w: int, ratio: float = 1.10) -> int:
    """字太多就縮小字級，讓它塞得進安全寬度。

    中文字寬約等於字級，`ratio` 是放大效果（pop）的預留。
    """
    n = max(1, len(text or ""))
    need = n * base * ratio
    if need <= max_w:
        return base
    return max(int(base * 0.55), int(base * max_w / need))


def profile() -> dict:
    """目前的產業風格檔。免費版只有通用款，進階規則包會帶更多。"""
    r = load_rules()
    name = r.get("style.profile", "general")
    presets = r.get("style.presets", {}) or {}
    return {"name": name, **(presets.get(name) or {})}


def header(w: int = W, h: int = H, styles: Optional[List[str]] = None) -> str:
    """產 ASS 檔頭。"""
    base = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {w}", f"PlayResY: {h}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    base += styles or []
    base += ["", "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
             "MarginV, Effect, Text"]
    return "\n".join(base) + "\n"


def style_line(name: str, size: int, colour: str, outline: float = 5.0,
               shadow: float = 1.4, align: int = 2, margin_v: int = 400,
               outline_colour: str = PALETTE["black"], bold: int = -1) -> str:
    f = TYPE["font"]
    return (f"Style: {name},{f},{size},{colour},{colour},{outline_colour},"
            f"{PALETTE['black']},{bold},0,0,0,100,100,0,0,1,{outline},{shadow},"
            f"{align},60,60,{margin_v},1")
