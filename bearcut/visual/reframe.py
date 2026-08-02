# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""取景 —— 從高解析素材裁一塊放大到輸出解析度。

## 「拉近鏡頭」與「橫轉直」是同一件事

兩者都是「從原圖框出一塊區域、放大到輸出尺寸」。差別只在框的比例：
拉近是同比例的小框，橫轉直是 9:16 的窄框。所以用同一套程式碼。

## 4K 素材是關鍵

從 4K（3840×2160）框出 1080p 大小的區域放大 → **零畫質損失**，
因為那塊區域本來就有 1080p 的像素。從 1080p 素材裁切放大則會糊。

所以拍攝時用 4K，後期就有了「免費的第二機位」。
"""

from typing import Callable, Dict, Optional, Tuple

from .. import media


def probe_size(video: str) -> Tuple[int, int]:
    """讀影片解析度。"""
    r = media.ffprobe(["-select_streams", "v", "-show_entries",
                       "stream=width,height", "-of", "csv=p=0", video])
    try:
        w, h = (int(x) for x in (r.stdout or "").strip().split(",")[:2])
        return w, h
    except (ValueError, TypeError):
        return 0, 0


def suggest(src_w: int, src_h: int, out_w: int = 1080, out_h: int = 1920,
            zoom: float = 1.0) -> Dict[str, int]:
    """算出預設的取景框（置中）。

    `zoom > 1` 代表拉更近（框更小）。回 `{x, y, w, h}`（原圖座標）。
    """
    if src_w <= 0 or src_h <= 0:
        return {"x": 0, "y": 0, "w": out_w, "h": out_h}

    ar = out_w / out_h
    # 先取「在來源裡能放下的最大目標比例框」，再依 zoom 縮小
    if src_w / src_h > ar:
        h = src_h
        w = int(h * ar)
    else:
        w = src_w
        h = int(w / ar)
    w = max(16, int(w / max(1.0, zoom)))
    h = max(16, int(h / max(1.0, zoom)))
    w -= w % 2
    h -= h % 2
    return {"x": (src_w - w) // 2, "y": (src_h - h) // 2, "w": w, "h": h}


def quality_note(box: Dict[str, int], out_w: int = 1080) -> str:
    """這個取景框會不會掉畫質。使用者拉框時需要即時知道。"""
    if box["w"] >= out_w:
        return f"零畫質損失（框寬 {box['w']}px ≥ 輸出 {out_w}px）"
    ratio = box["w"] / out_w
    if ratio >= 0.8:
        return f"輕微放大（框寬 {box['w']}px，約 {ratio:.0%}），畫質幾乎無損"
    return (f"⚠ 框太小（{box['w']}px，只有輸出的 {ratio:.0%}），"
            "放大後會糊。建議用 4K 素材或把框拉大。")


def render(video: str, out_path: str, box: Dict[str, int],
           out_w: int = 1080, out_h: int = 1920, crf: int = 18,
           progress_cb: Optional[Callable] = None) -> str:
    """依取景框裁切並縮放輸出。"""
    def report(p, m):
        if progress_cb:
            progress_cb(p, m)

    vf = (f"crop={box['w']}:{box['h']}:{box['x']}:{box['y']},"
          f"scale={out_w}:{out_h}:flags=lanczos")
    report(96, f"取景輸出（{box['w']}×{box['h']} → {out_w}×{out_h}）…")
    r = media.ffmpeg(["-y", "-i", video, "-vf", vf,
                      "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                      "-pix_fmt", "yuv420p", "-c:a", "copy", out_path])
    if r.returncode != 0:
        # 音訊 copy 有時不相容（來源編碼特殊），退回重編音訊
        r = media.ffmpeg(["-y", "-i", video, "-vf", vf,
                          "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                          out_path])
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-10:]
        raise RuntimeError("取景輸出失敗：\n" + "\n".join(tail))
    report(99, f"取景完成：{out_path}")
    return out_path
