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

import os
from typing import Callable, Dict, List, Optional, Tuple

# ── 版位 ────────────────────────────────────────────────────────
# 每個回傳 (x 運算式, y 運算式)。w/h 是畫面尺寸，lw/lh 是圖的尺寸。

def _p_full(w, h, lw, lh):
    """整頁：圖佔滿畫面。切走的那種，圖自己會帶背景。"""
    return f"(W-w)/2", f"(H-h)/2"


def _p_upper(w, h, lw, lh):
    """上半部：壓在畫面上方，講者的臉通常在中下，不會被蓋到。"""
    return "(W-w)/2", str(max(60, int(h * 0.16)))


# 頭頂上方的空白區：卡片要放得下、又不能碰到頭
TOP_MARGIN = 70
HEAD_GAP = 46
MIN_READABLE_ZOOM = 0.62   # 縮到比這還小就看不清楚了，那就別硬塞


def fit_above_head(w: int, h: int, tpl_w: int, tpl_h: int,
                   head_top: Optional[float] = None,
                   min_zoom: Optional[float] = None
                   ) -> Optional[Tuple[float, int]]:
    """卡片能不能放在頭上方而不蓋到人。回 `(縮放, y)`，放不下回 None。

    一個數字沒必要佔掉整個版面——熊董的原話是「沒必要占這麼多版面吧？
    你把我的人直接丟到最下面變成超擠了」。所以先試「他維持全螢幕，
    卡片放在頭上方的空白」，真的塞不下才整頁切走。

    `min_zoom` 是這支模板的最小可用比例，**由模板自己宣告**。
    每支能縮的程度差很多：主打數字縮到 0.6 還是很大，縮圖牆縮到 0.9
    標籤就只剩十幾像素、完全看不清楚。用同一個門檻會讓密的模板變成一團糊。
    """
    if not tpl_w or not tpl_h:
        return None
    floor = MIN_READABLE_ZOOM if min_zoom is None else float(min_zoom)
    limit = (h * 0.36 if head_top is None else head_top) - HEAD_GAP
    avail = limit - TOP_MARGIN
    if avail < 120:
        return None
    z = min(w * 0.94 / tpl_w, avail / tpl_h)
    if z < floor:
        return None            # 縮到看不清楚，不如整頁切走
    z = min(z, 1.6)
    y = int(TOP_MARGIN + (avail - tpl_h * z) / 2)
    return z, max(TOP_MARGIN, y)


def fit_full(w: int, h: int, tpl_w: int, tpl_h: int) -> float:
    """整頁切走時模板要放多大。人不在畫面上，所以可以放得比較開。"""
    if not tpl_w or not tpl_h:
        return 1.0
    return max(0.5, min(2.0, min(w * 0.90 / tpl_w, h * 0.62 / tpl_h)))


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
    """硬切。剪輯師最常用的一種，乾脆俐落。

    參考片就是這樣：前一格還是人像，下一格已經整個換版，中間沒有過渡。
    """
    return ""


def _t_fade(s, e, d, w, h):
    """淡入淡出。接在說話中間不會打斷節奏。"""
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

DEFAULT_TRANSITION = "cut"
DEFAULT_PLACEMENT = "upper"
DEFAULT_TRANS_SEC = 0.28


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


FULL_BG = "0x06080A"


def full_spans(layers: List[dict]) -> List[Tuple[float, float]]:
    """哪幾段時間是「整頁切走」——畫面只有素材，人不出現。

    相鄰的合併：兩張圖靠得很近時，中間不該閃回人像再切回去。
    """
    spans = sorted((float(v["start"]), float(v["end"]))
                   for v in layers
                   if (v.get("meta") or {}).get("placement") == "full")
    out: List[Tuple[float, float]] = []
    for s, e in spans:
        if out and s <= out[-1][1] + 0.4:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _expr(spans: List[Tuple[float, float]], invert: bool = False) -> str:
    if not spans:
        return "1" if invert else "0"
    j = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in spans)
    return f"not({j})" if invert else j


def build(layers: List[dict], w: int, h: int, base_label: str = "base",
          out_label: str = "gfx", first_input: int = 1,
          face_center: Optional[float] = None,
          assets_dir: Optional[str] = None) -> Tuple[str, List[str]]:
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

    # 「整頁切走」：那幾秒鋪一層不透明底幕把人蓋掉，素材獨佔畫面。
    # 這是剪輯師插 B-roll 的做法——要嘛人在畫面上、要嘛整個切走，
    # 不要把人壓成一小條（熊董：「換舞台也不必把人切到低於一半吧？」）。
    #
    # 底幕用 color+drawgrid 濾鏡生，不要用 `-loop 1` 餵 PNG：靜態圖當輸入是
    # 無限長的流，overlay 的 shortest 收不住，ffmpeg 會一路編下去
    # （這個坑踩過兩次，709MB 與 1.77GB）。
    fspans = full_spans(layers)
    if fspans:
        step = max(40, w // 18)
        frags.append(f"color=c={FULL_BG}:s={w}x{h}:r=24,"
                     f"drawgrid=w={step}:h={step}:t=1:c=0x12161C@1[fbg]")
        frags.append(f"[{base_label}][fbg]overlay=x=0:y=0:shortest=1:"
                     f"enable='{_expr(fspans)}'[fv]")
        prev = "fv"

    layer0 = first_input

    for i, L in enumerate(layers, start=1):
        idx = layer0 + i - 1
        inputs += ["-framerate", str(int(L.get("fps") or 24)),
                   "-i", L["frames_pattern"]]
        s, e = float(L["start"]), float(L["end"])
        lw, lh = int(L["w"]), int(L["h"])
        cfg = plan(L, w, h)
        d = cfg["trans_sec"]

        # 圖層：套轉場，再位移到它該出現的秒數。
        # **不要 tpad**——PNG 序列已經涵蓋整個顯示時間（含退場動畫），
        # 再撐就會在退場之後多出一段靜止的殘影。
        chain = []
        t = TRANSITIONS[cfg["transition"]](s, e, d, w, h)
        if t:
            chain.append(t)
        chain.append(f"setpts=PTS-STARTPTS+{s:.3f}/TB")
        frags.append(f"[{idx}:v]" + ",".join(chain) + f"[L{i}]")

        if L.get("y") is not None:
            # 呼叫端算好的位置（例如「放在頭上方剛好不蓋到人」）優先
            x, y = "(W-w)/2", str(int(L["y"]))
        else:
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
