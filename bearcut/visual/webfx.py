# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""HTML/CSS 動畫渲染器：把網頁動畫燒成透明圖層疊到影片上。

## 為什麼需要這一層

ASS 的向量繪圖畫得出方塊與折線，畫不出**圓角卡片、漸層、光暈、環形圖、
面積填充、模糊**。那些正是「看起來很專業」與「看起來像陽春字幕」的差別。
瀏覽器把這些做到爛熟，沒有理由自己重造。

## 這條路最漂亮的地方：模板是**資料**不是程式碼

    引擎（開源）      這支渲染器：吃 HTML 模板 + 資料 → 透明 PNG 序列 → 疊上去
    Pro 規則包（付費） 模板庫，每月新增

所以**每個月推新動畫不需要發新版程式**——模板走既有的規則包通道下發，
跟 prompt、字卡樣式同一條路。設計師也能直接貢獻模板，不必碰 Python。

## 不多下載一個瀏覽器

Windows 10/11 內建 Edge，而 Edge 是 Chromium 核心，支援 headless 截圖與
透明背景（`--default-background-color=00000000`）。找得到 Edge 或 Chrome
就直接用；都沒有才需要另外處理。**不為了這個功能扛 150MB 的相依。**

## 一次啟動渲染所有格

每格開一次瀏覽器要 0.7 秒，24fps × 2 秒就是 48 次啟動、半分多鐘。
改成把同一個模板**複製 N 份排成一長條**，用 Web Animations API 把每一份的
`currentTime` 設到不同時間再暫停，一次截圖拿到所有格，再用 ffmpeg 切開。

用 WAAPI 而不是覆寫 `animation-delay`，是因為模板裡常有錯開的延遲
（第二根柱子晚 0.08 秒長），覆寫會把那個錯落感洗掉；WAAPI 是directly
設時間軸位置，錯落原樣保留。
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from .. import media as _media

# 找瀏覽器的順序：系統既有的優先，最後才看 playwright 的快取
_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)

_browser: Optional[str] = None


class WebFxUnavailable(RuntimeError):
    """找不到瀏覽器。這不是錯誤，是「這台機器上做不了」——呼叫端要降級。"""


def find_browser(refresh: bool = False) -> Optional[str]:
    """找一個 Chromium 系瀏覽器。找不到回 None。"""
    global _browser
    if _browser and not refresh:
        return _browser
    for p in _CANDIDATES:
        if os.path.isfile(p):
            _browser = p
            return p
    for name in ("msedge", "chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            _browser = found
            return found
    # playwright 裝過的話也用它的
    cache = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if cache.is_dir():
        for exe in sorted(cache.glob("chromium-*/chrome-win/chrome.exe"), reverse=True):
            _browser = str(exe)
            return _browser
    return None


def available() -> bool:
    return find_browser() is not None


def describe() -> str:
    b = find_browser()
    return f"可用（{Path(b).name}）" if b else "找不到 Edge 或 Chrome，動畫會降級成基本樣式"


# ── 把模板包成「一長條 N 格」的頁面 ──────────────────────────────

_STRIP = """<!doctype html><meta charset="utf-8">
<style>
  html,body{{margin:0;padding:0;background:transparent}}
  .bc-strip{{display:block}}
  .bc-cell{{width:{w}px;height:{h}px;overflow:hidden;position:relative}}
  /* zoom 而不是 transform:scale——zoom 會放大版面單位，所以圓角、陰影、
     字距全部等比長大，出來還是原生解析度不會糊。模板本身用設計尺寸寫，
     要多大由呼叫端決定。 */
  .bc-cell > *{{zoom:{zoom}}}
</style>
<div class="bc-strip">{cells}</div>
<script>
  // 用 Web Animations API 把每一格的時間軸釘在不同位置再暫停。
  // 不覆寫 animation-delay：模板裡常有刻意錯開的延遲（第二根柱子晚一點長），
  // 覆寫會把那個錯落感洗掉。
  const FPS = {fps}, N = {n};
  document.querySelectorAll('.bc-cell').forEach((cell, i) => {{
    const t = (i / FPS) * 1000;
    cell.getAnimations({{subtree: true}}).forEach(a => {{
      try {{ a.currentTime = t; a.pause(); }} catch (e) {{}}
    }});
  }});
  document.title = 'ready';
</script>"""


def _fill(template: str, data: dict) -> str:
    """把 {{key}} 換成資料。故意用最笨的字串替換——模板是設計師寫的 HTML，
    引進樣板引擎只會多一個相依與一組新的注入風險。"""
    out = template
    for k, v in (data or {}).items():
        out = out.replace("{{" + str(k) + "}}", str(v))
    # 沒被填到的佔位符清掉，不要讓 {{title}} 出現在成品上
    return re.sub(r"\{\{[a-zA-Z0-9_]+\}\}", "", out)


def render_frames(template_html: str, out_dir: str, data: Optional[dict] = None,
                  w: int = 1000, h: int = 520, fps: int = 24, dur: float = 1.6,
                  timeout: int = 120, zoom: float = 1.0,
                  progress_cb: Optional[Callable] = None) -> List[str]:
    """把模板渲染成一串透明 PNG。回檔案路徑清單（已排序）。

    `zoom` 把模板等比放大再渲染（w/h 一起放大）。模板用設計尺寸寫，
    實際要在畫面上佔多大由呼叫端決定——舞台版型需要它佔滿舞台。
    """
    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    browser = find_browser()
    if not browser:
        raise WebFxUnavailable(
            "找不到 Edge 或 Chrome，沒辦法渲染 HTML 動畫。\n"
            "Windows 10/11 內建 Edge，通常不會缺；若真的沒有，"
            "裝一個 Chrome 就好，我們不會另外下載瀏覽器。")

    n = max(1, int(round(fps * dur)))
    zoom = max(0.25, float(zoom or 1.0))
    w, h = int(round(w * zoom)), int(round(h * zoom))
    body = _fill(template_html, data or {})
    cells = "".join(f'<div class="bc-cell">{body}</div>' for _ in range(n))
    page = _STRIP.format(w=w, h=h, fps=fps, n=n, cells=cells, zoom=zoom)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bearcut-fx-") as td:
        html = Path(td) / "strip.html"
        html.write_text(page, encoding="utf-8")
        strip = Path(td) / "strip.png"

        say(10, f"渲染 {n} 格動畫…")
        cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--hide-scrollbars", "--force-device-scale-factor=1",
               "--default-background-color=00000000",
               f"--window-size={w},{h * n}",
               # 給 CSS 動畫一點虛擬時間跑起來，腳本才有 animation 可以釘
               "--virtual-time-budget=1500",
               f"--screenshot={strip}", html.as_uri()]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if not strip.exists() or strip.stat().st_size < 1000:
            tail = (r.stderr or b"").decode("utf-8", "ignore")[-300:]
            raise WebFxUnavailable(f"瀏覽器沒有產出畫面。\n{tail}")

        say(60, "切成單格…")
        # 用 PIL 切，不用 ffmpeg。
        # ffmpeg 的 crop 只能用 `n`（幀號）當偏移量，而這裡的輸入是**一張** PNG，
        # n 恆為 0——切出來會是 16 張一模一樣的圖。踩過才發現。
        from PIL import Image
        im = Image.open(strip).convert("RGBA")
        for i in range(n):
            im.crop((0, i * h, w, (i + 1) * h)).save(out / f"f_{i+1:04d}.png")

    files = sorted(str(p) for p in out.glob("f_*.png"))
    say(100, f"{len(files)} 格完成")
    return files


def overlay(video: str, frames_dir: str, out_path: str,
            at: float, fps: int = 24,
            x: str = "(W-w)/2", y: str = "160",
            progress_cb: Optional[Callable] = None) -> str:
    """把 PNG 序列疊到影片的指定時間點上。回輸出路徑。

    最後一格會停留到動畫結束為止（`loop=1` 不行——那會整段重播），
    所以用 `tpad` 把序列補到需要的長度。
    """
    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    say(20, "把動畫疊上影片…")
    r = _media.ffmpeg([
        "-i", video,
        "-framerate", str(fps), "-i", os.path.join(frames_dir, "f_%04d.png"),
        "-filter_complex",
        # tpad：最後一格延續 2 秒，讓觀眾看得完
        f"[1:v]tpad=stop_mode=clone:stop_duration=2[fx];"
        f"[0:v][fx]overlay={x}:{y}:enable='gte(t,{at})':"
        f"eof_action=pass:shortest=0",
        "-c:a", "copy", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        out_path])
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-10:]
        raise RuntimeError("疊圖失敗：\n" + "\n".join(tail))
    say(100, "完成")
    return out_path


def templates(rulepack_dir: Optional[Path] = None) -> dict:
    """規則包裡有哪些動畫模板。回 `{名稱: {html, meta}}`。

    模板是資料不是程式碼，所以住在規則包裡——Pro 每月換一批新的，
    引擎不用動。每個 `x.html` 可以配一份同名的 `x.json` 宣告欄位；
    沒有宣告檔就當作所有欄位都是純文字、不做追溯驗證。
    """
    from ..rules import RULEPACK_DIR
    d = Path(rulepack_dir or RULEPACK_DIR) / "fx"
    if not d.is_dir():
        return {}
    out = {}
    for p in sorted(d.glob("*.html")):
        meta = {}
        side = p.with_suffix(".json")
        if side.exists():
            try:
                meta = {k: v for k, v in json.loads(
                    side.read_text(encoding="utf-8")).items() if not k.startswith("$")}
            except (json.JSONDecodeError, OSError):
                meta = {}
        out[p.stem] = {"html": str(p), "meta": meta}
    return out


def catalog(rulepack_dir: Optional[Path] = None) -> str:
    """給判斷腦看的模板清單（名稱、用途、要填哪些欄位）。"""
    lines = []
    for name, t in templates(rulepack_dir).items():
        m = t["meta"]
        fields = m.get("fields") or {}
        need = "、".join(
            f"{k}" + ("（要真數字）" if v.get("factual") else "")
            for k, v in fields.items())
        lines.append(f"- `{name}`：{m.get('desc', '')}\n  欄位：{need}")
    return "\n".join(lines) if lines else "（這個規則包沒有動畫模板）"
