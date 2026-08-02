# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""動態示意圖：依語意自動配圖表與動畫。

## 這一層在做什麼

大字卡把「他講的一句話」放大。這一層再往前一步：**把他講的內容畫出來**。

    他說「三個月從 0 做到五千粉」   → 一條長出來的成長曲線
    他說「有三個步驟」             → 三格依序亮起的流程
    他說「觀看掉了七成」           → 一條往下的長條
    他說「這件事只有兩種人」       → 左右對比

## ★ 鐵則：圖上的每個數字都要追得回逐字稿

這是「絕不信 LLM 秒數」的同一條紀律，換到數字上。

判斷腦只能說「第 12 段提到 5000，畫成長曲線」——那個 5000 **必須真的出現在
第 12 段的原文裡**（阿拉伯數字或中文數字都算）。`_verify()` 逐一比對，
對不上就整條丟掉，跟 `highlights._resolve()` 對不上段號就丟掉是同一個做法。

為什麼這條不能放寬：一個自動剪輯工具幫使用者生出**看起來很專業的假數據**，
那不是功能，是會讓他在自己的觀眾面前出事的風險。沒有數字可用時就走純示意
（流程、對比、強調），那類本來就沒有真假問題。

## 動畫怎麼做

ASS 支援向量繪圖（`\\p1`）與 `\\t` 補間，但 `\\t` 補不了繪圖路徑本身。
所以「長出來」的效果用**逐格事件**：同一個形狀切成十幾個短事件，每個畫得
高一點。10~14 格跑 0.5 秒，看起來就是連續的。

這條路的好處是完全不需要新的渲染管線——產出還是 ASS 事件，跟字幕、字卡
一起交給既有的 `vertical.render()`，一次燒錄完成。
"""

import re
from typing import Dict, List, Optional, Tuple

from ..llm import FAST, LLMUnavailable, Provider
from .style import ANCHORS, TYPE, fit_size, ts

# 動畫格數：越多越平滑，但事件行數也線性增加
STEPS = 12
# 一段動畫跑多久（秒）
GROW_SEC = 0.55
# 一個視覺停留多久（秒）
HOLD_SEC = 2.6

# 中文數字 → 阿拉伯，用來驗證「五千」對得上 5000
_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
       "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "萬": 10000, "億": 100000000}

SUPPORTED = ("counter", "bar", "progress", "ring", "trend", "compare", "steps")


def _cn_to_int(s: str) -> Optional[int]:
    """把「五千」「三成」「兩百五十」換成數字。看不懂回 None。"""
    s = s.strip()
    if not s or any(c not in _CN and c not in _CN_UNIT for c in s):
        return None
    total, section, digit = 0, 0, 0
    for c in s:
        if c in _CN:
            digit = _CN[c]
        else:
            unit = _CN_UNIT[c]
            if unit >= 10000:
                section = (section + (digit or 0)) * unit
                total += section
                section = digit = 0
            else:
                section += (digit or 1) * unit
                digit = 0
    return total + section + digit or None


def _numbers_in(text: str) -> List[float]:
    """抓出一段文字裡所有能對照的數字（阿拉伯 + 中文 + 成數）。"""
    out: List[float] = []
    for m in re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")):
        out.append(float(m))
    # 中文數字串
    for m in re.findall(r"[零〇一二兩三四五六七八九十百千萬億]+", text):
        v = _cn_to_int(m)
        if v:
            out.append(float(v))
    # 「七成」= 70%
    for m in re.findall(r"([零〇一二兩三四五六七八九十]+)成", text):
        v = _cn_to_int(m)
        if v:
            out.append(float(v * 10))
    return out


def _traceable(value: float, text: str) -> bool:
    """這個數字追得回這段原文嗎。

    容許 5% 誤差——「快五千」寫成 4800 之類的四捨五入是合理的，
    但不能讓模型憑空生一個好看的數字。
    """
    for n in _numbers_in(text):
        if n == value or (n and abs(n - value) / max(abs(n), 1) <= 0.05):
            return True
        # 5000 對「五千」、70 對「七成」都在 _numbers_in 處理過了
    return False


def _verify(v: dict, segments: List[dict]) -> Optional[dict]:
    """把判斷腦回的一條視覺翻成可用的規格。不合格回 None。"""
    try:
        i = int(v["seg"]) - 1
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= i < len(segments)):
        return None
    kind = str(v.get("type", "")).strip()
    if kind not in SUPPORTED:
        return None

    seg = segments[i]
    text = seg.get("text", "")

    # 帶數字的類型：每個數字都要追得回原文
    values: List[Tuple[str, float]] = []
    if kind in ("counter", "bar", "progress", "ring", "trend", "compare"):
        raw = v.get("items") or []
        if kind in ("counter", "progress", "ring") and v.get("value") is not None:
            raw = [{"label": v.get("label", ""), "value": v.get("value")}]
        for it in raw:
            if not isinstance(it, dict):
                continue
            try:
                val = float(it.get("value"))
            except (TypeError, ValueError):
                continue
            if not _traceable(val, text):
                return None          # 一個對不上就整條丟掉，不留半真半假的圖
            values.append((str(it.get("label", "")).strip()[:8], val))
        if not values:
            return None

    labels = [str(x).strip()[:10] for x in (v.get("steps") or []) if str(x).strip()]
    if kind == "steps" and len(labels) < 2:
        return None

    start = float(seg["start"])
    end = min(float(seg["end"]), start + HOLD_SEC + GROW_SEC)
    if end - start < 0.6:
        end = start + 0.6

    return {"seg": i, "type": kind, "start": round(start, 3), "end": round(end, 3),
            "title": str(v.get("title", "")).strip()[:14],
            "unit": str(v.get("unit", "")).strip()[:4],
            "values": values, "steps": labels,
            "source": text[:40]}


def has_judgment() -> bool:
    """規則包裡有沒有這個功能的判準。用來決定要不要跟使用者提一句。"""
    from .. import rules as _rules
    from ..rules import RulepackError
    try:
        _rules.load().prompt("visuals", count=0, numbered="", max_n=0, types="")
        return True
    except RulepackError:
        return False
    except Exception:
        return True          # 其他錯誤不該被誤判成「沒有 Pro」


def pick(segments: List[dict], llm: Provider,
         max_n: int = 5, progress_cb=None) -> List[dict]:
    """讀逐字稿，決定哪幾句要配什麼圖。判準來自規則包。"""
    from .. import rules as _rules

    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not segments:
        return []
    numbered = "\n".join(f"{i+1}. {s.get('text','')}" for i, s in enumerate(segments))
    # 判準住在 Pro 規則包。免費包沒有這份 prompt——這個功能整個屬於 Pro。
    # 回空清單而不是丟例外：短影音的其他部分（字幕、字卡、封面）照做，
    # 只是少了圖。呼叫端負責告訴使用者為什麼少。
    from ..rules import RulepackError
    try:
        prompt = _rules.load().prompt("visuals", count=len(segments),
                                      numbered=numbered, max_n=max_n,
                                      types="、".join(SUPPORTED))
    except RulepackError:
        return []
    say(52, "判斷腦挑要配圖的句子…")
    try:
        data = llm.complete_json(prompt, tier=FAST)
    except LLMUnavailable:
        return []

    out, used = [], set()
    for v in (data.get("visuals") or []):
        if not isinstance(v, dict):
            continue
        spec = _verify(v, segments)
        if not spec or spec["seg"] in used:
            continue
        used.add(spec["seg"])
        out.append(spec)
        if len(out) >= max_n:
            break
    say(56, f"配了 {len(out)} 個動態示意圖")
    return out


# ───────────────────────── ASS 繪圖 ─────────────────────────

def _rect(x: int, y: int, w: int, h: int) -> str:
    """ASS 向量矩形（\\p1 座標）。"""
    return f"m {x} {y} l {x+w} {y} l {x+w} {y+h} l {x} {y+h}"


def _ease(t: float) -> float:
    """out-cubic：一開始快、收尾慢，比線性有生氣。"""
    return 1 - (1 - t) ** 3


def _frames(start: float, dur: float) -> List[Tuple[float, float, float]]:
    """回 [(事件起, 事件訖, 進度 0~1)]。"""
    out = []
    step = dur / STEPS
    for k in range(STEPS):
        t0 = start + k * step
        out.append((t0, t0 + step * 1.35, _ease((k + 1) / STEPS)))
    return out


def _fmt(v: float, unit: str = "") -> str:
    s = f"{int(v):,}" if abs(v - int(v)) < 0.01 else f"{v:,.1f}"
    return s + unit


def events(visuals: List[dict], w: int = 1080) -> List[str]:
    """把視覺規格轉成 ASS 事件行。跟字卡一樣掛在畫面上半部。"""
    cx = w // 2
    y = ANCHORS.get("card_top_y", 520)
    ev: List[str] = []

    for v in visuals:
        s, e = v["start"], v["end"]
        gs = min(GROW_SEC, max(0.3, (e - s) * 0.45))
        title = v["title"]
        if title:
            fs = fit_size(title, TYPE.get("card_top_size", 64), 900)
            ev.append(f"Dialogue: 1,{ts(s)},{ts(e)},CardTop,,0,0,0,,"
                      f"{{\\pos({cx},{y - 150})\\fs{fs}\\fad(120,140)}}{title}")

        kind = v["type"]
        if kind == "counter":
            ev += _counter(v, cx, y, s, e, gs)
        elif kind in ("bar", "compare"):
            ev += _bars(v, cx, y, s, e, gs)
        elif kind in ("progress", "ring"):
            ev += _progress(v, cx, y, s, e, gs)
        elif kind == "trend":
            ev += _trend(v, cx, y, s, e, gs)
        elif kind == "steps":
            ev += _steps(v, cx, y, s, e, gs)
    return ev


def _counter(v, cx, y, s, e, gs) -> List[str]:
    """數字從 0 跳到目標值。"""
    label, target = v["values"][0]
    out = []
    for t0, t1, p in _frames(s, gs):
        out.append(f"Dialogue: 3,{ts(t0)},{ts(t1)},CardKey,,0,0,0,,"
                   f"{{\\pos({cx},{y})\\fs150\\an5}}{_fmt(target * p, v['unit'])}")
    # 跳完之後定格顯示到結束，順便彈一下
    out.append(f"Dialogue: 3,{ts(s + gs)},{ts(e)},CardKey,,0,0,0,,"
               f"{{\\pos({cx},{y})\\fs150\\an5\\t(0,120,\\fscx112\\fscy112)"
               f"\\t(120,220,\\fscx100\\fscy100)}}{_fmt(target, v['unit'])}")
    if label:
        out.append(f"Dialogue: 2,{ts(s)},{ts(e)},CardTop,,0,0,0,,"
                   f"{{\\pos({cx},{y + 110})\\fs52\\an5\\fad(120,140)}}{label}")
    return out


def _bars(v, cx, y, s, e, gs) -> List[str]:
    """長條圖：由下往上長。compare 用同一組繪圖，只是通常兩根。"""
    vals = v["values"][:5]
    top = max(x[1] for x in vals) or 1
    n = len(vals)
    bw, gap, hmax = 118, 46, 300
    total = n * bw + (n - 1) * gap
    x0 = cx - total // 2
    base = y + hmax // 2
    out = []
    for i, (label, val) in enumerate(vals):
        bx = x0 + i * (bw + gap)
        full = max(12, int(hmax * (val / top)))
        for t0, t1, p in _frames(s + i * 0.06, gs):
            h = max(4, int(full * p))
            out.append(f"Dialogue: 3,{ts(t0)},{ts(t1)},CardKey,,0,0,0,,"
                       f"{{\\pos(0,0)\\p1\\an7\\bord0\\shad0}}"
                       f"{_rect(bx, base - h, bw, h)}{{\\p0}}")
        out.append(f"Dialogue: 3,{ts(s + gs + i * 0.06)},{ts(e)},CardKey,,0,0,0,,"
                   f"{{\\pos(0,0)\\p1\\an7\\bord0\\shad0}}"
                   f"{_rect(bx, base - full, bw, full)}{{\\p0}}")
        out.append(f"Dialogue: 4,{ts(s + gs)},{ts(e)},CardKey,,0,0,0,,"
                   f"{{\\pos({bx + bw // 2},{base - full - 34})\\fs50\\an5\\fad(100,120)}}"
                   f"{_fmt(val, v['unit'])}")
        if label:
            out.append(f"Dialogue: 4,{ts(s)},{ts(e)},CardTop,,0,0,0,,"
                       f"{{\\pos({bx + bw // 2},{base + 46})\\fs42\\an5\\fad(120,140)}}{label}")
    return out


def _progress(v, cx, y, s, e, gs) -> List[str]:
    """進度條 / 百分比。ring 也走這條——圓環用 ASS 畫弧不穩，改用等效的橫條，
    視覺目的（呈現比例）一樣達成，而且在各家播放器都畫得出來。"""
    label, val = v["values"][0]
    pct = max(0.0, min(100.0, val))
    W, H = 700, 46
    x0, base = cx - W // 2, y
    out = [f"Dialogue: 2,{ts(s)},{ts(e)},CardKey,,0,0,0,,"
           f"{{\\pos(0,0)\\p1\\an7\\bord0\\shad0\\alpha&H99&}}"
           f"{_rect(x0, base, W, H)}{{\\p0}}"]
    for t0, t1, p in _frames(s, gs):
        fw = max(6, int(W * (pct / 100) * p))
        out.append(f"Dialogue: 3,{ts(t0)},{ts(t1)},CardKey,,0,0,0,,"
                   f"{{\\pos(0,0)\\p1\\an7\\bord0\\shad0}}"
                   f"{_rect(x0, base, fw, H)}{{\\p0}}")
    fw = max(6, int(W * (pct / 100)))
    out.append(f"Dialogue: 3,{ts(s + gs)},{ts(e)},CardKey,,0,0,0,,"
               f"{{\\pos(0,0)\\p1\\an7\\bord0\\shad0}}"
               f"{_rect(x0, base, fw, H)}{{\\p0}}")
    out.append(f"Dialogue: 4,{ts(s + gs)},{ts(e)},CardKey,,0,0,0,,"
               f"{{\\pos({cx},{base - 60})\\fs104\\an5}}{_fmt(pct, '%')}")
    if label:
        out.append(f"Dialogue: 4,{ts(s)},{ts(e)},CardTop,,0,0,0,,"
                   f"{{\\pos({cx},{base + H + 52})\\fs44\\an5\\fad(120,140)}}{label}")
    return out


def _trend(v, cx, y, s, e, gs) -> List[str]:
    """折線：一段一段長出來。用細長矩形當線段，避免各家對 ASS 線寬的差異。"""
    vals = [x[1] for x in v["values"]][:8]
    if len(vals) < 2:
        return []
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    # 圖整體往下讓開標題：折線的最高點會落在 base-H，如果那個位置跟標題重疊
    # （標題在 y-150），線就會壓在字上。實測過，所以基準線再往下推。
    W, H = 760, 230
    x0, base = cx - W // 2, y + H // 2 + 70
    step = W // (len(vals) - 1)
    pts = [(x0 + i * step, base - int(H * (val - lo) / span))
           for i, val in enumerate(vals)]

    out = []
    per = gs / max(1, len(pts) - 1)
    for i in range(len(pts) - 1):
        (ax, ay), (bx, by) = pts[i], pts[i + 1]
        t0 = s + i * per
        for k in range(1, 5):
            p = k / 4
            mx, my = int(ax + (bx - ax) * p), int(ay + (by - ay) * p)
            out.append(f"Dialogue: 3,{ts(t0 + per * (k - 1) / 4)},{ts(e)},CardKey,,0,0,0,,"
                       f"{{\\pos(0,0)\\p1\\an7\\bord0\\shad0}}"
                       f"{_seg_rect(ax, ay, mx, my)}{{\\p0}}")
    # 每個轉折點都標值——只標末端的話，觀眾看不出「從多少漲到多少」，
    # 而那正是折線圖唯一想講的事。中間的點小一點，不要跟末端搶。
    for i, ((px, py), val) in enumerate(zip(pts, vals)):
        last = i == len(pts) - 1
        at = s + gs if last else s + per * i + per
        out.append(f"Dialogue: 4,{ts(at)},{ts(e)},CardKey,,0,0,0,,"
                   f"{{\\pos({px},{py})\\fs{40 if last else 26}\\an5"
                   f"\\t(0,140,\\fscx130\\fscy130)\\t(140,240,\\fscx100\\fscy100)}}●")
        # 末端的數字放上方，中間的放下方，免得跟線本身打架
        dy = -56 if last else 40
        out.append(f"Dialogue: 4,{ts(at)},{ts(e)},CardKey,,0,0,0,,"
                   f"{{\\pos({px},{py + dy})\\fs{54 if last else 36}\\an5\\fad(140,0)}}"
                   f"{_fmt(val, v['unit'] if last else '')}")
    return out


def _seg_rect(ax: int, ay: int, bx: int, by: int, thick: int = 7) -> str:
    """兩點之間的粗線段，用四邊形表示（ASS 沒有可靠的線寬）。"""
    import math
    dx, dy = bx - ax, by - ay
    ln = math.hypot(dx, dy) or 1
    ox, oy = -dy / ln * thick, dx / ln * thick
    return (f"m {int(ax+ox)} {int(ay+oy)} l {int(bx+ox)} {int(by+oy)} "
            f"l {int(bx-ox)} {int(by-oy)} l {int(ax-ox)} {int(ay-oy)}")


def _steps(v, cx, y, s, e, gs) -> List[str]:
    """流程／清單：一格一格亮起來。沒有數字，所以不需要追溯驗證。"""
    labels = v["steps"][:4]
    n = len(labels)
    bw, gap = 230, 34
    total = n * bw + (n - 1) * gap
    x0 = cx - total // 2
    per = gs / n
    out = []
    for i, lab in enumerate(labels):
        bx = x0 + i * (bw + gap)
        t0 = s + i * per
        out.append(f"Dialogue: 2,{ts(t0)},{ts(e)},CardKey,,0,0,0,,"
                   f"{{\\pos(0,0)\\p1\\an7\\bord0\\shad0\\alpha&HAA&\\fad(100,0)}}"
                   f"{_rect(bx, y - 60, bw, 120)}{{\\p0}}")
        out.append(f"Dialogue: 3,{ts(t0)},{ts(e)},CardTop,,0,0,0,,"
                   f"{{\\pos({bx + bw // 2},{y - 18})\\fs44\\an5\\fad(120,0)}}{i + 1}")
        out.append(f"Dialogue: 3,{ts(t0)},{ts(e)},CardTop,,0,0,0,,"
                   f"{{\\pos({bx + bw // 2},{y + 26})\\fs38\\an5\\fad(120,0)}}{lab}")
    return out
