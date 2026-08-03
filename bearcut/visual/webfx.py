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

#: 一張長條圖的最大高度。瀏覽器的截圖高度大約卡在 16384px，超過就整片空白
#: 而且不報錯。留一點餘裕。
MAX_STRIP_PX = 14000

_STRIP = """<!doctype html><meta charset="utf-8">
<style>
  /* 配色代號。模板只寫 var(--bc-accent)，實際顏色由主題決定，
     所以換一套配色不用改任何一支模板。主題住在規則包的 fx/_themes.json。 */
  :root{{{tokens}}}
  html,body{{margin:0;padding:0;background:transparent}}
  .bc-strip{{display:block}}
  .bc-cell{{width:{w}px;height:{h}px;overflow:visible;position:relative}}
  /* zoom 而不是 transform:scale——zoom 會放大版面單位，所以圓角、陰影、
     字距全部等比長大，出來還是原生解析度不會糊。模板本身用設計尺寸寫，
     要多大由呼叫端決定。 */
  .bc-life{{zoom:{zoom}}}

  /* ── 生命週期：進場、停留、退場 ──────────────────────────
     模板只寫進場的細節編排，整體的「來與走」統一在這裡做。

     為什麼要有這一層：以前只算進場那幾格，之後複製最後一格撐著，時間到
     直接切掉——圖是**突然消失**的，看起來很生硬。這裡讓每張圖都有收尾，
     而且不用改任何一支模板。

     退場比進場快（0.26s vs 0.34s）：東西要走得比來得果斷，
     拖泥帶水的退場會讓節奏黏住。

     ⚠️ 外層的 `linear` 一定要留著——百分比位置編碼的是「停留多久」，
     換成曲線會把停留時間也一起扭曲。真正的緩動寫在各段的
     `animation-timing-function` 裡（CSS 允許逐段指定）。
     整條時間軸等速的話，觀眾先看到的是整張卡片等速平移，
     模板內部那些精心編排的 cubic-bezier 全被這層信封蓋掉。

     模糊只到 4px 而且在 68% 就收掉：模糊屬於「效果類」屬性，
     要比空間位移更早結束。跟位移一起跑滿全程的話，前 1/3 是一團糊影。 */
  .bc-life{{
    animation: bc-life {total:.3f}s linear both;
    transform-origin: 50% 30%;
  }}
  @keyframes bc-life {{
    0%      {{ opacity:0; transform: translateY(34px) scale(.955); filter: blur(4px);
               animation-timing-function: cubic-bezier(.05,.7,.1,1) }}
    {in_b}% {{ opacity:1; transform: translateY(-4px) scale(1.008); filter: blur(0);
               animation-timing-function: cubic-bezier(.2,0,0,1) }}
    {in_a}% {{ opacity:1; transform: translateY(0)    scale(1);     filter: blur(0);
               animation-timing-function: linear }}
    {out_a}%{{ opacity:1; transform: translateY(0)    scale(1);     filter: blur(0);
               animation-timing-function: cubic-bezier(.3,0,.8,.15) }}
    100%    {{ opacity:0; transform: translateY(-20px) scale(.985); filter: blur(3px) }}
  }}
</style>
<div class="bc-strip">{cells}</div>
<script>
  // 用 Web Animations API 把每一格的時間軸釘在不同位置再暫停。
  // 不覆寫 animation-delay：模板裡常有刻意錯開的延遲（第二根柱子晚一點長），
  // 覆寫會把那個錯落感洗掉。
  // OFFSET：這一批是整段動畫的第幾格開始。分批算的時候不加這個，
  // 每一批都會從頭播一次，成片上就是動畫重播好幾次。
  const FPS = {fps}, N = {n}, OFFSET = {offset};
  document.querySelectorAll('.bc-cell').forEach((cell, i) => {{
    const t = ((OFFSET + i) / FPS) * 1000;
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
                  theme: Optional[str] = None,
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
    # 進場 0.34 秒、退場 0.26 秒，其餘時間停著。換算成百分比給 keyframes。
    tokens = theme_css(theme)
    in_a = min(45.0, 0.34 / max(dur, 0.6) * 100)
    in_b = in_a * 0.68          # 不透明度與模糊在空間位移之前先收掉
    out_a = max(in_a + 5, 100 - 0.26 / max(dur, 0.6) * 100)

    # ⚠️ 一張長條圖裝得下幾格是有上限的。瀏覽器的截圖高度大約卡在 16384px，
    # 超過的部分**整片空白而且不會報錯**——實測 4.7 秒 ×24fps ×544px 高
    # 需要 61,000px，於是第 30 格之後全空，成片上那張圖就消失了。
    # 所以分批算，每批控制在安全高度以內。
    per = max(1, min(n, MAX_STRIP_PX // max(1, h)))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    done = 0
    with tempfile.TemporaryDirectory(prefix="bearcut-fx-") as td:
        for batch, base in enumerate(range(0, n, per)):
            cnt = min(per, n - base)
            cells = "".join(
                f'<div class="bc-cell"><div class="bc-life">{body}</div></div>'
                for _ in range(cnt))
            page = _STRIP.format(w=w, h=h, fps=fps, n=cnt, cells=cells,
                                 zoom=zoom, total=dur, offset=base,
                                 tokens=tokens,
                                 in_a=f"{in_a:.1f}", in_b=f"{in_b:.1f}",
                                 out_a=f"{out_a:.1f}")
            html = Path(td) / f"strip{batch}.html"
            html.write_text(page, encoding="utf-8")
            strip = Path(td) / f"strip{batch}.png"

            say(10 + int(80 * base / n), f"渲染第 {base+1}~{base+cnt} 格…")
            r = subprocess.run(
                [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--hide-scrollbars", "--force-device-scale-factor=1",
                 "--default-background-color=00000000",
                 f"--window-size={w},{h * cnt}",
                 # 給 CSS 動畫一點虛擬時間跑起來，腳本才有 animation 可以釘
                 "--virtual-time-budget=1500",
                 f"--screenshot={strip}", html.as_uri()],
                capture_output=True, timeout=timeout)
            if not strip.exists() or strip.stat().st_size < 1000:
                tail = (r.stderr or b"").decode("utf-8", "ignore")[-300:]
                raise WebFxUnavailable(f"瀏覽器沒有產出畫面。\n{tail}")

            # 用 PIL 切，不用 ffmpeg。
            # ffmpeg 的 crop 只能用 `n`（幀號）當偏移量，而這裡的輸入是**一張**
            # PNG，n 恆為 0——切出來會是一模一樣的圖。踩過才發現。
            im = Image.open(strip).convert("RGBA")
            if im.height < h * cnt - 2:
                raise WebFxUnavailable(
                    f"瀏覽器只畫出 {im.height}px，需要 {h * cnt}px。"
                    f"單格高度 {h}px 可能太大。")
            for i in range(cnt):
                im.crop((0, i * h, w, (i + 1) * h)).save(
                    out / f"f_{base + i + 1:04d}.png")
            done += cnt

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


#: 配色代號的預設值。規則包沒有 _themes.json 時用這組——
#: 少了主題檔不該讓動畫整個變透明，那比配色不對嚴重得多。
_FALLBACK_THEME = {
    "bg": "#080D18", "bg2": "#141F33", "surface": "#16233A",
    "line": "rgba(255,255,255,.08)", "text": "#FFFFFF", "text2": "#8B909C",
    "accent": "#FFC800", "accent2": "#FF7D0A", "accent3": "#F0D060",
    "good": "#60C878", "bad": "#FF5A5A",
    "ink": "#16181D", "paper": "#F7F3EA",
}


def themes(rulepack_dir: Optional[Path] = None) -> dict:
    """規則包裡有哪些配色。回 `{名字: {代號: 顏色}}`。"""
    from ..rules import RULEPACK_DIR
    p = Path(rulepack_dir or RULEPACK_DIR) / "fx" / "_themes.json"
    if not p.exists():
        return {"midnight": dict(_FALLBACK_THEME)}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"midnight": dict(_FALLBACK_THEME)}
    out = {}
    for name, t in (d.get("themes") or {}).items():
        if name.startswith("$"):
            continue
        out[name] = {k: v for k, v in t.items() if not k.startswith("$")}
    return out or {"midnight": dict(_FALLBACK_THEME)}


def theme_css(name: Optional[str] = None,
              rulepack_dir: Optional[Path] = None) -> str:
    """把配色轉成 CSS 變數宣告。找不到指定的配色就用預設，不要壞掉。"""
    ts = themes(rulepack_dir)
    from ..rules import RULEPACK_DIR
    default = "midnight"
    p = Path(rulepack_dir or RULEPACK_DIR) / "fx" / "_themes.json"
    if p.exists():
        try:
            default = json.loads(p.read_text(encoding="utf-8")).get(
                "default", default)
        except (json.JSONDecodeError, OSError):
            pass
    t = ts.get(name or default) or ts.get(default) or next(iter(ts.values()))
    merged = dict(_FALLBACK_THEME)
    merged.update(t)                      # 主題沒定義的代號用預設補齊
    return "".join(f"--bc-{k}:{v};" for k, v in merged.items())


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
