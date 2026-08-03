# -*- coding: utf-8 -*-
"""插入原件：畫面上要出現一段圖的時候，怎麼進、怎麼出、佔多少畫面。

拆成原件，是因為「配什麼圖」跟「怎麼放進畫面」是兩件事。
判斷腦負責前者，這裡負責後者。要換一種進場方式，加一個函式就好，
不用動判斷腦、不用動模板、不用動剪接流程。

三種原件互相獨立、可以自由組合：

  版位 PLACEMENTS   圖佔畫面的哪裡、多大
  轉場 TRANSITIONS  怎麼進來、怎麼出去
  時機（呼叫端決定）什麼時候進、待多久

⚠️ 這裡刻意**不做**上下分割版型。實測過把人像縮進底部小窗，熊董的回饋是
「硬卡一個上下畫面，整個很不順暢」「把我切到下方，我人整個被蓋住」。
剪輯師的做法是切走再切回（B-roll），不是把主角縮小。
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

# ── 版位 ────────────────────────────────────────────────────────
# 每個回傳 (x 運算式, y 運算式)。w/h 是畫面尺寸，lw/lh 是圖的尺寸。

def _p_full(w, h, lw, lh):
    """整頁：圖佔滿畫面。切走的那種，圖自己會帶背景。"""
    return f"(W-w)/2", f"(H-h)/2"


def _p_upper(w, h, lw, lh):
    """上半部：壓在畫面上方，講者的臉通常在中下，不會被蓋到。"""
    return "(W-w)/2", str(max(60, int(h * 0.16)))


def _p_lower_third(w, h, lw, lh):
    """下三分之一：新聞條的位置，字幕上方。"""
    return "(W-w)/2", str(max(60, int(h * 0.58)))


PLACEMENTS: Dict[str, Callable] = {
    "full": _p_full,
    "upper": _p_upper,
    "lower_third": _p_lower_third,
}


# ── 轉場 ────────────────────────────────────────────────────────
# 每個回傳一段接在圖層後面的 filter 字串（不含前後的 [標籤]）。
# s/e 是這張圖的進出時間，d 是轉場長度。

def _t_cut(s, e, d, w, h):
    """硬切。剪輯師最常用的一種，乾脆俐落。"""
    return ""


def _t_fade(s, e, d, w, h):
    """淡入淡出。安全牌，接在說話中間不會打斷節奏。"""
    return (f"fade=t=in:st=0:d={d:.2f}:alpha=1,"
            f"fade=t=out:st={max(0.0, (e - s) - d):.2f}:d={d:.2f}:alpha=1")


def _t_slide_up(s, e, d, w, h):
    """由下往上推進來。配合「數字往上長」的內容很順。"""
    return (f"fade=t=in:st=0:d={d * .6:.2f}:alpha=1,"
            f"fade=t=out:st={max(0.0, (e - s) - d):.2f}:d={d:.2f}:alpha=1")


TRANSITIONS: Dict[str, Callable] = {
    "cut": _t_cut,
    "fade": _t_fade,
    "slide_up": _t_slide_up,
}

# 位移式轉場要動 overlay 的座標，跟淡入不同層，所以另外查表。
# 回傳 (x 運算式, y 運算式)，`t0` 會被換成這張圖的起始秒數。
_MOTION: Dict[str, Callable] = {
    "slide_up": lambda x, y, d: (
        x, f"({y})+(1-min(1,(t-{{t0}})/{d:.2f}))*90"),
}

DEFAULT_TRANSITION = "fade"
DEFAULT_PLACEMENT = "upper"
DEFAULT_TRANS_SEC = 0.32


def plan(visual: dict, w: int, h: int) -> dict:
    """一張圖要怎麼放進畫面。

    優先用模板自己宣告的（`placement` / `transition` 寫在 .json 裡，
    模板作者最清楚這張圖該怎麼進場），沒宣告才用預設。
    """
    meta = visual.get("meta") or {}
    place = str(meta.get("placement") or DEFAULT_PLACEMENT)
    trans = str(meta.get("transition") or DEFAULT_TRANSITION)
    if place not in PLACEMENTS:
        place = DEFAULT_PLACEMENT
    if trans not in TRANSITIONS:
        trans = DEFAULT_TRANSITION
    return {"placement": place, "transition": trans,
            "trans_sec": float(meta.get("trans_sec") or DEFAULT_TRANS_SEC)}


def build(layers: List[dict], w: int, h: int, base_label: str = "base",
          out_label: str = "gfx", first_input: int = 1) -> Tuple[str, List[str]]:
    """把所有插入層組成 filter 片段。回 (filter 字串, 額外輸入參數)。

    每一層一組 setpts（位移到它該出現的秒數）＋轉場＋overlay。
    **時間位移不能省**——少了它，第 24 秒才要出現的圖，到那個時間點
    序列早就播完了，畫面上什麼都不會有（舊版就是這樣，還回報「疊上 4 個」）。
    """
    if not layers:
        return "", []

    frags: List[str] = []
    inputs: List[str] = []
    prev = base_label
    for i, L in enumerate(layers, start=1):
        idx = first_input + i - 1
        inputs += ["-framerate", str(int(L.get("fps") or 24)),
                   "-i", L["frames_pattern"]]
        s, e = float(L["start"]), float(L["end"])
        lw, lh = int(L["w"]), int(L["h"])
        cfg = plan(L, w, h)
        d = cfg["trans_sec"]

        # 圖層：先撐到需要的長度，再套轉場，最後位移到它該出現的秒數
        chain = [f"tpad=stop_mode=clone:stop_duration={max(0.1, e - s):.3f}"]
        t = TRANSITIONS[cfg["transition"]](s, e, d, w, h)
        if t:
            chain.append(t)
        chain.append(f"setpts=PTS-STARTPTS+{s:.3f}/TB")
        frags.append(f"[{idx}:v]" + ",".join(chain) + f"[L{i}]")

        x, y = PLACEMENTS[cfg["placement"]](w, h, lw, lh)
        mv = _MOTION.get(cfg["transition"])
        if mv:
            x, y = mv(x, y, d)
            x, y = x.format(t0=f"{s:.3f}"), y.format(t0=f"{s:.3f}")
        nxt = f"{out_label}{i}"
        # x/y/enable 一律用具名參數並加單引號。運算式裡的逗號（min(1,…)、
        # between(t,…)）不加引號會被當成 filter 參數分隔符，整段 filter 解析失敗。
        frags.append(f"[{prev}][L{i}]overlay=x='{x}':y='{y}':"
                     f"eof_action=pass:repeatlast=0:"
                     f"enable='between(t,{s:.3f},{e:.3f})'[{nxt}]")
        prev = nxt

    frags.append(f"[{prev}]null[{out_label}]")
    return ";".join(frags), inputs
