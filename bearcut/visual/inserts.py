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


def _p_lower_third(w, h, lw, lh):
    """下三分之一：新聞條的位置，字幕上方。"""
    return "(W-w)/2", str(max(60, int(h * 0.58)))


PLACEMENTS: Dict[str, Callable] = {
    "full": _p_full,
    "upper": _p_upper,
    "lower_third": _p_lower_third,
    "stage": _p_full,       # 舞台版位的座標由 build() 另外算，見下方說明
}

# ── 舞台版位 ────────────────────────────────────────────────────
# 跟其他版位不同：它會動到**底片本身**（人像縮進下方小窗），不只是疊一層。
# 所以座標算在 build() 裡，這張表只登記名字。
#
# 比例逐格量自參考片（720×1280）：
#   小窗上緣 0.719、滿寬左右各留 2.8%、字幕 0.641、背景近黑
#
# ⚠️ 兩個一定要做對，做錯就是熊董說的「把我切到下方，我人整個被蓋住」：
#   1. **換版是硬切**，一格切完。參考片 x_010 還是全螢幕、x_011 就整個換版了，
#      中間沒有過渡。淡入淡出會讓兩個版面糊在一起，看起來很髒。
#   2. **裁切要對準臉**。窗是寬扁的，從直式原片隨便取一條會切掉額頭或下巴。
#      有臉部偵測就用偵測到的位置，沒有才退回經驗值。
STAGE_WIN_TOP = 0.719
STAGE_SIDE_PAD = 0.028
STAGE_SUB_Y = 0.641
STAGE_BG = "0x06080A"
STAGE_FACE_CENTER = 0.45        # 偵測不到臉時的退路
STAGE_FILL_W = 0.94             # 圖要佔畫面寬度的比例


def stage_zoom(w: int, h: int, tpl_w: int, tpl_h: int) -> float:
    """模板要放大幾倍才會撐滿舞台。模板寫設計尺寸，實際大小由版位決定。

    寬高兩邊都不能爆，取比較嚴的那個。用 CSS zoom 放大（不是事後拉伸），
    所以圓角、陰影、字距等比長大，出來還是原生解析度。
    """
    if not tpl_w or not tpl_h:
        return 1.0
    stage_h = stage_subtitle_y(h) - 90        # 上方留邊 + 字幕上方留白
    return max(0.5, min(2.5, min(w * STAGE_FILL_W / tpl_w, stage_h / tpl_h)))


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


def stage_assets(w: int, h: int, out_dir: str) -> bool:
    """產人像小窗的圓角遮罩。回是否成功。

    只剩遮罩——底圖改用 ffmpeg 的 color+drawgrid 直接生，不再需要外部檔案。
    圓角遮罩沒有濾鏡等價物，所以還是畫一張；但它走 `movie` 濾鏡源讀進來，
    不佔輸入編號、也不會變成無限長的流。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    os.makedirs(out_dir, exist_ok=True)
    _, _, ww, wh = stage_rect(w, h)
    m = Image.new("L", (ww, wh), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, ww - 1, wh - 1], radius=30,
                                        fill=255)
    m.convert("RGB").save(os.path.join(out_dir, "stage_mask.png"))
    return True


def stage_rect(w: int, h: int) -> Tuple[int, int, int, int]:
    """舞台版位的人像小窗 (x, y, w, h)。"""
    pad = int(round(w * STAGE_SIDE_PAD))
    y = int(round(h * STAGE_WIN_TOP))
    return pad, y, w - pad * 2, h - y


def stage_subtitle_y(h: int) -> int:
    """舞台版位時字幕該放的高度（小窗上方）。"""
    return int(round(h * STAGE_SUB_Y))


def stage_spans(layers: List[dict], pad: float = 0.0) -> List[Tuple[float, float]]:
    """哪幾段時間要換成舞台版面。相鄰的合併，免得畫面閃回去又切回來。

    `pad` 預設 0：**換版是硬切**，前後不留緩衝。留了就會在人還在講話的時候
    先換版，看起來像卡了一下。
    """
    spans = sorted((max(0.0, float(v["start"]) - pad), float(v["end"]) + pad)
                   for v in layers
                   if (v.get("meta") or {}).get("placement") == "stage")
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

    # ⚠️ filter 裡的 `[n:v]` 一定要跟 `-i` 的實際順序對得起來。
    # 之前底圖與遮罩用 `-i` 餵進來時算錯過編號，結果把不透明的網格底圖當成
    # 一張動畫疊上去，畫面上四張圖全糊在一起、人整個不見，而 ffmpeg 回傳 0。
    # 現在底圖與遮罩都改走濾鏡源（color / movie），不佔輸入編號，
    # 所以圖層編號就是單純的遞增——這種錯不會再發生。
    spans = stage_spans(layers)
    layer0 = first_input

    # ── 舞台版位：底片本身要換版 ────────────────────────────
    if spans:
        wx, wy, ww, wh = stage_rect(w, h)
        fc = STAGE_FACE_CENTER if face_center is None else float(face_center)
        # 小窗是寬扁的，原片是直式，所以裁一條橫幅出來，中心對準臉
        crop_h = max(2, min(h, int(round(ww and w * wh / ww or wh))))
        crop_y = max(0, min(h - crop_h, int(round(h * fc - crop_h / 2))))
        bg_png = os.path.join(assets_dir, "stage_bg.png") if assets_dir else None
        mask_png = os.path.join(assets_dir, "stage_mask.png") if assets_dir else None

        # 底圖用 ffmpeg 內建的 color+drawgrid 生，**不要用 `-loop 1` 餵 PNG**。
        # 靜態圖當輸入是無限長的流，overlay 的 shortest 收不住，ffmpeg 會一路
        # 編下去——這個坑踩過兩次（42 分鐘 709MB、以及 1.77GB）。
        # 濾鏡生出來的 color 源會跟著主軌結束，沒有這個問題。
        step = max(40, w // 18)
        frags.append(f"color=c={STAGE_BG}:s={w}x{h}:r=24,"
                     f"drawgrid=w={step}:h={step}:t=1:c=0x12161C@1[sbg]")

        frags.append(f"[{base_label}]split=2[sfull][sforwin]")
        frags.append(f"[sforwin]crop={w}:{crop_h}:0:{crop_y},"
                     f"scale={ww}:{wh}[swin]")
        if mask_png and os.path.exists(mask_png):
            # 遮罩是單張圖，用 movie 讀進來當濾鏡源，同樣避開 `-loop` 輸入。
            # 路徑用專案現成的 esc()——Windows 的 `C:` 冒號會被當成參數分隔符。
            from .style import esc as _esc
            frags.append(f"movie='{_esc(mask_png)}',scale={ww}:{wh},"
                         f"format=gray[smk]")
            frags.append("[swin][smk]alphamerge[swin2]")
        else:
            frags.append("[swin]null[swin2]")
        # 硬切：enable 直接開關，沒有淡入淡出。剪輯師就是這樣切的。
        frags.append(f"[sbg][sfull]overlay=x=0:y=0:shortest=1:"
                     f"enable='{_expr(spans, invert=True)}'[sv0]")
        frags.append(f"[sv0][swin2]overlay=x={wx}:y={wy}:"
                     f"enable='{_expr(spans)}'[sv1]")
        prev = "sv1"

    for i, L in enumerate(layers, start=1):
        idx = layer0 + i - 1
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

        if cfg["placement"] == "stage":
            # 置中在「畫面頂端 ~ 字幕」那塊空出來的舞台，不是整個畫面置中
            y = max(40, (stage_subtitle_y(h) - 60 - lh) // 2)
            x, y = "(W-w)/2", str(y)
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
