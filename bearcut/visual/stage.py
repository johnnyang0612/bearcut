# -*- coding: utf-8 -*-
"""舞台版型：動畫上台的時候，人像讓位。

為什麼要有這一層——參考了業界做得好的那種短影音之後得到的結論：
**差別不在卡片畫得漂不漂亮，在版型。**

把卡片貼在人臉上，不管卡片做得多精緻，看起來都像後製硬蓋上去的貼紙。
做得好的是這樣：動畫出現的那幾秒，整個畫面換版——人像縮進底部的圓角小窗，
上方整塊留給動畫當舞台，字幕移到兩者中間。動畫講完，畫面換回全螢幕人像。

版型比例量自參考片（720×1280）：
    人像小窗上緣 0.72、滿寬、圓角
    字幕（中文）0.634、（英文）0.662
    背景 RGB(6,8,8) 近黑

另外這裡順手修掉舊 overlay 的一個致命問題：舊版把 PNG 序列直接餵給 overlay
再用 `enable='gte(t,at)'` 控制何時顯示，但序列本身沒有做時間位移——所以第 24
秒才要出現的動畫，在那個時間點序列早就播完了。實測四個動畫只有第一個看得到，
而程式還回報「疊上 4 個」。這裡每一段都用 setpts 位移到它該出現的時間。
"""
from __future__ import annotations

import os
from typing import Callable, List, Optional, Tuple

from .. import media

# 版型比例（相對畫面高度），量自參考片
WIN_TOP = 0.72          # 人像小窗上緣
SUB_Y = 0.645           # 舞台模式下字幕的垂直位置
STAGE_CENTER = 0.31     # 舞台區的視覺中心
FACE_CENTER = 0.36      # 從直式原片裁人像小窗時，以這個高度為中心（對齊臉）
STAGE_FILL = 0.94       # 動畫要佔畫面寬度的比例——留一點邊，但要有存在感
BG = "0x06080A"


def zoom_for(w: int, tpl_w: int) -> float:
    """模板要放大幾倍才會撐滿舞台。模板用設計尺寸寫，實際大小在這裡決定。"""
    if not tpl_w:
        return 1.0
    return max(0.5, min(2.5, (w * STAGE_FILL) / tpl_w))


def window_rect(w: int, h: int) -> Tuple[int, int, int, int]:
    """人像小窗的位置與大小 (x, y, w, h)。滿寬靠底。"""
    y = int(round(h * WIN_TOP))
    return 0, y, w, h - y


def subtitle_y(h: int) -> int:
    """舞台模式下字幕該放的高度。"""
    return int(round(h * SUB_Y))


def windows(visuals: List[dict], pad: float = 0.25) -> List[Tuple[float, float]]:
    """哪幾段時間要切成舞台版型。前後各留一點讓換版不要太急。

    重疊的區間會合併——兩個動畫靠得很近時，畫面不該在中間閃回全螢幕再切回去。
    """
    spans = sorted((max(0.0, float(v["start"]) - pad), float(v["end"]) + pad)
                   for v in visuals)
    out: List[Tuple[float, float]] = []
    for s, e in spans:
        if out and s <= out[-1][1] + 0.35:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _backdrop(w: int, h: int, path: str) -> Optional[str]:
    """畫舞台背景：近黑底加一層很淡的網格。

    純色底會顯得空。參考片在背景鋪了一層幾乎看不見的網格，眼睛不會注意到它，
    但少了它整塊黑會塌下去。線要夠淡——看得出來就太重了。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    bg = (0x06, 0x08, 0x0A)
    im = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(im)
    step = max(40, w // 18)
    line = (0x12, 0x16, 0x1C)
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=line, width=1)
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=line, width=1)
    im.save(path)
    return path


def _window_mask(ww: int, wh: int, path: str, radius: int = 34) -> Optional[str]:
    """人像小窗的圓角遮罩。直角小窗看起來像沒做完。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    m = Image.new("L", (ww, wh), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, ww - 1, wh - 1],
                                        radius=radius, fill=255)
    m.convert("RGB").save(path)
    return path


def _enable_expr(spans: List[Tuple[float, float]], invert: bool = False) -> str:
    """把時間區間轉成 ffmpeg 的 enable 運算式。"""
    if not spans:
        return "0" if not invert else "1"
    joined = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in spans)
    return f"not({joined})" if invert else joined


def compose(video: str, out_path: str, layers: List[dict],
            fps: int = 24,
            progress_cb: Optional[Callable] = None) -> str:
    """一次 ffmpeg 完成所有動畫的合成。回輸出路徑。

    `layers` 每一項：{frames_dir, start, end, w, h}

    一次做完而不是一個動畫重編一次——舊版每疊一個就整支重新編碼一遍，
    四個動畫等於四次有損轉檔，而且慢。
    """
    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not layers:
        return video
    from .reframe import probe_size
    w, h = probe_size(video)
    wx, wy, ww, wh = window_rect(w, h)
    spans = windows(layers)

    # 小窗是寬扁的，原片是直式，所以要從原片裁一條橫幅出來。
    # 裁切中心對準臉大約所在的高度——用畫面正中會裁到胸口以下。
    crop_h = max(2, min(h, int(round(w * wh / ww))))
    crop_y = max(0, min(h - crop_h, int(round(h * FACE_CENTER - crop_h / 2))))

    import tempfile
    aux = tempfile.mkdtemp(prefix="bearcut-stage-")
    bg_png = _backdrop(w, h, os.path.join(aux, "bg.png"))
    mask_png = _window_mask(ww, wh, os.path.join(aux, "mask.png"))

    args: List[str] = ["-i", video]
    idx = {}
    n = 1
    if bg_png:
        args += ["-loop", "1", "-i", bg_png]
        idx["bg"] = n
        n += 1
    if mask_png:
        args += ["-loop", "1", "-i", mask_png]
        idx["mask"] = n
        n += 1
    for L in layers:
        args += ["-framerate", str(fps), "-i",
                 os.path.join(L["frames_dir"], "f_%04d.png")]
        L["_in"] = n
        n += 1

    fc = []
    if bg_png:
        fc.append(f"[{idx['bg']}:v]scale={w}:{h}[bg]")
    else:
        fc.append(f"color=c={BG}:s={w}x{h}:r={fps}[bg]")
    fc.append("[0:v]split=2[full][forwin]")
    fc.append(f"[forwin]crop={w}:{crop_h}:0:{crop_y},scale={ww}:{wh}[smallraw]")
    if mask_png:
        # 圓角：把遮罩併成 alpha，直角小窗看起來像沒做完
        fc.append(f"[{idx['mask']}:v]scale={ww}:{wh},format=gray[mk]")
        fc.append("[smallraw][mk]alphamerge[small]")
    else:
        fc.append("[smallraw]null[small]")
    fc += [
        # 全螢幕人像：舞台時間以外才畫
        f"[bg][full]overlay=0:0:shortest=1:"
        f"enable='{_enable_expr(spans, invert=True)}'[v0]",
        # 人像小窗：舞台時間才畫
        f"[v0][small]overlay={wx}:{wy}:enable='{_enable_expr(spans)}'[v1]",
    ]

    prev = "v1"
    for i, L in enumerate(layers, start=1):
        s, e = float(L["start"]), float(L["end"])
        hold = max(0.1, e - s)
        lw, lh = int(L["w"]), int(L["h"])
        # 置中在「畫面頂端 ~ 字幕」這塊舞台區裡，不是用固定比例——
        # 卡片高度差很多（主打數字 720、清單 620），固定比例會讓矮的偏上、高的爆出去
        y = max(24, int(round((subtitle_y(h) - 60 - lh) / 2)))
        # setpts 把整段位移到它該出現的時間——舊版漏了這一步，
        # 導致除了第一個以外的動畫都在畫面上看不到。
        fc.append(f"[{L['_in']}:v]setpts=PTS-STARTPTS+{s:.3f}/TB,"
                  f"tpad=stop_mode=clone:stop_duration={hold:.3f}[fx{i}]")
        nxt = f"v1_{i}"
        fc.append(f"[{prev}][fx{i}]overlay={(w - lw) // 2}:{y}:"
                  f"eof_action=pass:repeatlast=0:"
                  f"enable='between(t,{s:.3f},{e:.3f})'[{nxt}]")
        prev = nxt

    fc.append(f"[{prev}]format=yuv420p[vout]")

    say(30, f"合成 {len(layers)} 個動畫與舞台版型…")
    try:
        r = media.ffmpeg(args + [
            "-filter_complex", ";".join(fc),
            "-map", "[vout]", "-map", "0:a?",
            "-c:a", "copy", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", out_path])
    finally:
        import shutil
        shutil.rmtree(aux, ignore_errors=True)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-12:]
        raise RuntimeError("舞台合成失敗：\n" + "\n".join(tail))
    say(100, "完成")
    return out_path
