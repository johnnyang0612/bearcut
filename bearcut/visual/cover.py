# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""封面 —— 抽一幀 + 疊標題。

## 封面決定點擊率，比片子本身還重要

實測有效的做法：
- **疑問句鉤子**比陳述句好（讓人想知道答案）
- **封面問問題、片頭給答案** —— 兩者要接得上
- 標題**分三帶**：主標最大、副標次之、品牌名最小

## 抽哪一幀

不抽第 0 秒（常是還沒開口的空鏡），抽全片 1/5 處——那時講者通常已經進入狀態、
表情自然。避開接縫前後 0.5 秒，免得抽到轉場的中間幀。
"""

import os
from typing import List, Optional, Tuple

from .. import media
from .style import PALETTE

# 三帶佈局（相對於封面高度的比例）
BANDS = {"main": 0.40, "sub": 0.56, "brand": 0.90}


def _pick_time(duration: float, keep: Optional[List[dict]] = None) -> float:
    """挑抽幀時間點。"""
    t = max(1.0, duration * 0.2)
    if not keep:
        return t
    # 避開接縫附近（轉場中間幀常是模糊的）
    acc = 0.0
    seams = []
    for k in keep[:-1]:
        acc += k["end"] - k["start"]
        seams.append(acc)
    for _ in range(8):
        if all(abs(t - s) > 0.5 for s in seams):
            return t
        t += 0.7
    return t


def grab(video: str, out: str, duration: Optional[float] = None,
         keep: Optional[List[dict]] = None, width: int = 1080) -> Optional[str]:
    """抽一幀當封面底圖。"""
    if duration is None:
        try:
            duration = media.get_duration(video)
        except Exception:
            duration = 10.0
    t = _pick_time(duration, keep)
    r = media.ffmpeg(["-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
                      "-vf", f"scale={width}:-2", "-q:v", "2", "-y", out])
    return out if r.returncode == 0 and os.path.exists(out) else None


_font_warned = False


def _font(size: int):
    """載入內建字型。

    ⚠️ **失敗時要出聲。** Pillow 的預設字型不含中文，退回去會靜默產出一排豆腐字
    ——而這種錯只有打開封面圖才看得到。實測就這樣產過一張全是黑方塊的封面。
    """
    global _font_warned
    from PIL import ImageFont
    from ..env.platform import ROOT

    p = ROOT / "assets" / "fonts" / "SourceHanSansTC-Heavy.otf"
    try:
        return ImageFont.truetype(str(p), size)
    except OSError:
        if not _font_warned:
            _font_warned = True
            print(f"⚠ 載不到內建字型（{p}），封面文字會變成方塊。\n"
                  "  請確認 assets/fonts/ 裡有 SourceHanSansTC-Heavy.otf。")
        return ImageFont.load_default()


def _draw_text(draw, text: str, cx: int, y: int, size: int,
               fill=(255, 255, 255), stroke=(0, 0, 0), stroke_w: int = 6):
    """置中畫一行帶描邊的字。描邊是必要的——封面底圖亮暗不可控。"""
    f = _font(size)
    try:
        bbox = draw.textbbox((0, 0), text, font=f, stroke_width=stroke_w)
        w = bbox[2] - bbox[0]
    except AttributeError:
        w = len(text) * size
    draw.text((cx - w // 2, y), text, font=f, fill=fill,
              stroke_width=stroke_w, stroke_fill=stroke)


def make(video: str, out: str, main: str, sub: str = "", brand: str = "",
         duration: Optional[float] = None, keep: Optional[List[dict]] = None,
         vertical: bool = True) -> Optional[str]:
    """產封面：抽幀 + 疊三帶標題。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "bearcut_cover_frame.jpg")
    if not grab(video, tmp, duration, keep):
        return None

    try:
        img = Image.open(tmp).convert("RGB")
    except Exception:
        return None

    # 直式封面裁成 9:16，橫式維持原比例
    if vertical:
        tw, th = 1080, 1920
        scale = max(tw / img.width, th / img.height)
        img = img.resize((int(img.width * scale), int(img.height * scale)))
        left = (img.width - tw) // 2
        top = (img.height - th) // 3          # 偏上取景，臉比較不會被切
        img = img.crop((left, top, left + tw, top + th))
    else:
        tw, th = img.width, img.height

    # 壓一層暗色讓字浮出來——底圖亮的時候白字會看不見
    dark = Image.new("RGB", (tw, th), (0, 0, 0))
    img = Image.blend(img, dark, 0.28)

    d = ImageDraw.Draw(img)
    cx = tw // 2
    if main:
        _draw_text(d, main[:12], cx, int(th * BANDS["main"]),
                   size=int(tw * 0.115), stroke_w=8)
    if sub:
        _draw_text(d, sub[:16], cx, int(th * BANDS["sub"]),
                   size=int(tw * 0.062), fill=(255, 200, 0), stroke_w=6)
    if brand:
        _draw_text(d, brand[:14], cx, int(th * BANDS["brand"]),
                   size=int(tw * 0.038), fill=(220, 220, 220), stroke_w=4)

    img.save(out, "JPEG", quality=92)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return out
