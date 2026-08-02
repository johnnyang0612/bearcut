# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""追講者 —— 偵測畫面裡的人臉，裁到當下正在講話的那個人。

## 這是選用的加強，不是預設

追講者滿版視覺上更好，但**它會漏人**：雙人對談時裁到 A，B 講話的反應就看不到。
而且臉部偵測失敗時必須有退路。所以預設是「不裁切、模糊填底」，
這一層偵測不到臉就自動退回去。

## 兩個 OpenCV 的坑

1. **鎖 4.x**：OpenCV 5.0 拿掉了內建的 Haar cascade 與 CascadeClassifier。
2. **cv2 開不了非 ASCII 路徑**：中文檔名會直接讀不到。所以 Haar 檔要複製到
   ASCII 暫存目錄再載入，影片也要先用 ffmpeg 抽幀到 ASCII 暫存再餵給 cv2。
"""

import os
import shutil
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

from .. import media


def available() -> bool:
    """OpenCV 有沒有裝、而且版本對。"""
    try:
        import cv2
        return int(cv2.__version__.split(".")[0]) == 4
    except Exception:
        return False


def _cascade():
    """載入內建 Haar 人臉分類器。

    cv2 讀不了非 ASCII 路徑（中文使用者名稱很常見），所以先複製到
    ASCII 暫存目錄再載入。
    """
    import cv2
    src = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
    if not os.path.exists(src):
        return None
    dst = os.path.join(tempfile.gettempdir(), "bearcut_haar.xml")
    try:
        if not os.path.exists(dst):
            shutil.copyfile(src, dst)
        c = cv2.CascadeClassifier(dst)
        return None if c.empty() else c
    except Exception:
        return None


def _sample_frames(video: str, n: int = 12) -> List[str]:
    """抽幾張幀到 ASCII 暫存目錄（cv2 讀不了中文路徑）。"""
    try:
        dur = media.get_duration(video)
    except Exception:
        return []
    d = tempfile.mkdtemp(prefix="bearcut_faces_")
    out = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        p = os.path.join(d, f"f{i:02d}.jpg")
        r = media.ffmpeg(["-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
                          "-vf", "scale=640:-2", "-y", p])
        if r.returncode == 0 and os.path.exists(p):
            out.append(p)
    return out


def detect_faces(video: str, progress_cb: Optional[Callable] = None) -> List[Dict]:
    """偵測畫面裡穩定出現的人臉位置。

    回 `[{x, y, w, h}]`（相對 640 寬的取樣幀座標），偵測不到回空清單。
    """
    def report(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not available():
        report(96, "沒有安裝 OpenCV 4.x，追講者停用（改用不裁切版面）")
        return []

    cascade = _cascade()
    if cascade is None:
        report(96, "載不到人臉分類器，追講者停用")
        return []

    import cv2
    frames = _sample_frames(video)
    if not frames:
        return []

    # 收集所有幀的人臉，取出現最穩定的位置
    hits: List[Tuple[int, int, int, int]] = []
    for p in frames:
        img = cv2.imread(p)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for (x, y, w, h) in cascade.detectMultiScale(gray, 1.15, 5, minSize=(60, 60)):
            hits.append((int(x), int(y), int(w), int(h)))

    for p in frames:                       # 清掉暫存幀
        try:
            os.unlink(p)
        except OSError:
            pass

    if not hits:
        report(96, "偵測不到人臉，改用不裁切版面（雙人對談不會漏人）")
        return []

    # 依水平位置分群——雙人對談會分成左右兩群
    hits.sort(key=lambda f: f[0])
    groups: List[List[Tuple[int, int, int, int]]] = [[hits[0]]]
    for f in hits[1:]:
        if f[0] - groups[-1][-1][0] < 120:
            groups[-1].append(f)
        else:
            groups.append([f])

    faces = []
    for g in groups:
        if len(g) < max(2, len(frames) // 4):
            continue                       # 出現次數太少，多半是誤判
        faces.append({
            "x": sum(f[0] for f in g) // len(g),
            "y": sum(f[1] for f in g) // len(g),
            "w": sum(f[2] for f in g) // len(g),
            "h": sum(f[3] for f in g) // len(g),
            "hits": len(g),
        })

    report(96, f"偵測到 {len(faces)} 個穩定人臉位置")
    return faces


def crop_box(faces: List[Dict], src_w: int, src_h: int,
             out_w: int = 1080, out_h: int = 1920) -> Optional[Dict[str, int]]:
    """依人臉位置算出直式裁切框。

    只有**單人**時才裁——雙人以上裁了會漏人，回 None 讓呼叫端用不裁切版面。
    """
    if len(faces) != 1:
        return None

    f = faces[0]
    scale = src_w / 640.0                  # 取樣幀是 640 寬
    fx = (f["x"] + f["w"] / 2) * scale      # 臉中心（原圖座標）

    ar = out_w / out_h
    h = src_h
    w = int(h * ar)
    if w > src_w:
        w = src_w
        h = int(w / ar)
    w -= w % 2
    h -= h % 2

    x = int(fx - w / 2)
    x = max(0, min(x, src_w - w))          # 夾在畫面內
    y = max(0, (src_h - h) // 2)
    return {"x": x, "y": y, "w": w, "h": h}
