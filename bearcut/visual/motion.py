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
# 示意圖的標題畫在錨點上方 150px（字級最大 64，連描邊約占到 -114）。
# 任何圖形元素都不可以越過 -88 這條線，否則會壓在自己的標題上。
# 新增圖表型別時請照這條線算基準位置——_bars 就是沒算才把標題整個蓋掉。
CHART_TOP_PAD = 88
# 一個視覺停留多久（秒）
HOLD_SEC = 2.6
#: 兩張圖之間交疊多久。前一張的退場與後一張的進場在中段接住，
#: 觀眾感受到的是「換」而不是「消失，然後出現」。
OVERLAP_SEC = 0.15

# 中文數字 → 阿拉伯，用來驗證「五千」對得上 5000
_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
       "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "萬": 10000, "億": 100000000}

SUPPORTED = ("counter", "bar", "progress", "ring", "trend", "compare", "steps")
# fx = 用 HTML/CSS 模板渲染的精緻動畫（圓角、漸層、光暈、環形圖）。
# ASS 畫不出那些，所以走瀏覽器；模板住在規則包裡，是資料不是程式碼。
FX = "fx"


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
        # 概數：「六七百萬」是六百萬到七百萬，他兩個數字都講了，兩個都算數。
        # 不展開的話「欠了六七百萬」只解得出七百萬，寫六百萬就被判定成捏造。
        r = re.match(r"^([零〇一二兩三四五六七八九])([零〇一二兩三四五六七八九])"
                     r"([十百千萬億].*)$", m)
        if r:
            for d in (r.group(1), r.group(2)):
                v2 = _cn_to_int(d + r.group(3))
                if v2:
                    out.append(float(v2))
    # 「七成」= 70%
    for m in re.findall(r"([零〇一二兩三四五六七八九十]+)成", text):
        v = _cn_to_int(m)
        if v:
            out.append(float(v * 10))
    return out


def _unit_scale(unit: str) -> float:
    """單位裡的量級倍數：「萬元」→ 10000、「千萬」→ 10000000、「人」→ 1。"""
    scale = 1.0
    for c in unit or "":
        if c in _CN_UNIT and _CN_UNIT[c] >= 10:
            scale *= _CN_UNIT[c]
    return scale


def _traceable(value: float, text: str, unit: str = "") -> bool:
    """這個數字追得回這段原文嗎。

    容許 5% 誤差——「快五千」寫成 4800 之類的四捨五入是合理的，
    但不能讓模型憑空生一個好看的數字。

    `unit` 帶量級時（「萬元」）也試縮放後的值：畫面上寫「1000 萬元」比
    「10000000 元」好看得多，模型自然會那樣填，不能因此判它捏造。
    """
    scale = _unit_scale(unit)
    cands = {value} | ({value * scale} if scale != 1 else set())
    for n in _numbers_in(text):
        for value_ in cands:
            if n == value_ or (n and abs(n - value_) / max(abs(n), 1) <= 0.05):
                return True
        # 5000 對「五千」、70 對「七成」都在 _numbers_in 處理過了
    return False


def _human_number(num: float, unit: str) -> Tuple[float, str]:
    """把數字換成中文習慣的量級。6000000 → (600, "萬")。

    單位裡已經有量級字（「萬元」）就不再換算，否則會變成 600 萬萬。
    """
    if any(c in unit for c in "萬億千百"):
        return num, unit
    if abs(num) >= 100000000:
        return num / 100000000, "億" + unit
    if abs(num) >= 10000:
        return num / 10000, "萬" + unit
    return num, unit


def _pretty(num: float) -> str:
    """千分位。大數字沒有千分位就是一串 0，觀眾在畫面上一秒讀不出來。"""
    if abs(num - round(num)) < 0.01:
        return f"{int(round(num)):,}"
    return f"{num:,.1f}"


#: 模板欄位允許的標籤。只放行「標記重點」用的，不放行會載入外部東西的
#: （img/script/iframe/style）——模板是規則包裡的資料，但欄位值是判斷腦生的，
#: 兩者的信任等級不同。
_SAFE_TAG = re.compile(
    r"</?(?:span|b|strong|em|i|br)(?:\s+class=\"[\w\- ]{0,40}\")?\s*/?>",
    re.I)


def _safe_text(s: str, max_len: int) -> str:
    """截字並清掉不允許的標籤。

    長度只算**看得見的字**——大字報的重點詞要用 <span> 包起來，
    把標籤算進長度的話一包就超標被砍掉半個標籤，畫面直接壞掉。
    """
    kept = []
    visible = 0
    i = 0
    while i < len(s) and visible < max_len:
        m = _SAFE_TAG.match(s, i)
        if m:
            kept.append(m.group(0))
            i = m.end()
            continue
        if s[i] == "<":                     # 不在白名單的標籤，整個丟掉
            j = s.find(">", i)
            i = len(s) if j < 0 else j + 1
            continue
        kept.append(s[i])
        visible += 1
        i += 1
    return "".join(kept)


def _verify_fx(v: dict, segments: List[dict], tpls: dict) -> Optional[dict]:
    """驗一條 fx（HTML 模板）視覺。

    追溯只套在**畫面上會被讀成事實**的欄位（宣告檔裡 factual: true 的）。
    長條的相對高度沒有標數字，觀眾不會把它讀成一個事實，所以是裝飾、不驗——
    但只要有一個標了數字的欄位對不上原文，整條丟掉。
    """
    name = str(v.get("template", "")).strip()
    if name not in tpls:
        return None
    try:
        i = int(v["seg"]) - 1
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= i < len(segments)):
        return None

    meta = tpls[name]["meta"]
    spec_fields = meta.get("fields") or {}
    text = segments[i].get("text", "")
    # 跟 _verify 同一個理由：一句話常被字幕切成兩段，對比卡的兩個數字
    # 分屬前後段是常態。窗開到相鄰各一段，鐵則不變——仍然是他講過的數字。
    w_lo, w_hi = max(0, i - 1), min(len(segments), i + 2)
    window = "　".join(segments[j].get("text", "") for j in range(w_lo, w_hi))
    fields = v.get("fields") or {}
    if not isinstance(fields, dict):
        return None

    out_fields = {}
    unit_fix: dict = {}
    nums: dict = {}
    for key, decl in spec_fields.items():
        if key not in fields:
            continue
        val = fields[key]
        if decl.get("type") == "number":
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            # 模板的單位是獨立的文字欄位（big + unit、leftValue + leftUnit），
            # 由宣告檔的 unitField 指過去——「1000」配「萬」要對得上「破千萬」。
            unit = str(fields.get(decl.get("unitField") or "", "") or "")
            if decl.get("factual") and not _traceable(num, window, unit):
                return None          # 畫面上會被當事實的數字，對不上就整條丟掉
            lo, hi = decl.get("min"), decl.get("max")
            if lo is not None:
                num = max(float(lo), num)
            if hi is not None:
                num = min(float(hi), num)
            # 顯示格式在這裡定，不能指望判斷腦——它這輪填 1000 配「萬」，
            # 下輪就填 10000000 配空字串，畫面上變成一長串 0 沒人看得懂。
            # 追溯驗證已經在上面用原值做完了，這裡只動呈現。
            if decl.get("unitField"):
                num, unit = _human_number(num, unit)
                # 不能當場寫進 out_fields——單位本身也是欄位，迴圈跑到它時
                # 會用判斷腦的原值蓋回去。等整圈跑完再套。
                unit_fix[decl["unitField"]] = unit
            nums[key] = num              # 衍生欄位要算數，不能拿千分位字串去 float()
            out_fields[key] = _pretty(num)
        else:
            txt = _safe_text(str(val).strip(), int(decl.get("max") or 40))
            # 宣告檔可以限定合法值（例如 kind 只能是 doc/chat/video/…）。
            # 判斷腦很容易把「doc」寫成「文件」，填錯就用預設，
            # 不要原樣傳給模板——那會讓整面縮圖退回同一種樣式。
            allowed = decl.get("oneOf")
            if allowed and txt not in allowed:
                txt = str(decl.get("default") or allowed[0])
            out_fields[key] = txt

    if not out_fields:
        return None
    out_fields.update(unit_fix)

    # 衍生欄位（例如 ringDeg = ringPct * 3.6）由宣告檔算，判斷腦不用填
    for dk, expr in (meta.get("derived") or {}).items():
        if dk.startswith("$"):
            continue
        try:
            src, _, mul = str(expr).partition("*")
            base = nums.get(src.strip(), out_fields.get(src.strip()))
            if base is not None:
                out_fields[dk] = round(float(base) * float(mul.strip() or 1), 2)
        except (TypeError, ValueError):
            pass

    start = float(segments[i]["start"])
    dur = float(meta.get("dur") or 1.6)
    # 掛多久看動畫本身要畫多久，**不要被字幕段落的結尾夾住**。
    # 清單要 2.1 秒才畫完，卻掛在一句 1.7 秒的字幕上，畫到一半就消失了。
    # 圖比那句話活得久是正常的剪接——剪輯師切的 B-roll 也不會跟著句子斷。
    end = start + dur + HOLD_SEC
    last = float(segments[-1]["end"])
    if end > last:                      # 但不要超出影片結尾
        end = last
    if end - start < 0.8:
        end = start + 0.8
    return {"seg": i, "type": FX, "template": name,
            "start": round(start, 3), "end": round(end, 3),
            "fields": out_fields,
            "w": int(meta.get("w") or 1000), "h": int(meta.get("h") or 520),
            "y": int(meta.get("y") or 200), "dur": dur,
            "source": text[:40]}


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
    # 一句話常被 SRT 切成兩行——「以前要五個十個人團隊做的事情」／「現在就是
    # 我一個人來做」是同一個對比，數字卻分屬兩段。只查本段會把好圖整條丟掉。
    # 放寬到相鄰各一段：仍然是「他真的講過的數字」，鐵則沒破，只是不再要求
    # 落在同一行字幕。窗開到 ±1 就好，再大就會抓到不相干的數字。
    lo, hi = max(0, i - 1), min(len(segments), i + 2)
    window = "　".join(segments[j].get("text", "") for j in range(lo, hi))

    # 帶數字的類型：每個數字都要追得回原文
    values: List[Tuple[str, float]] = []
    reach = i                       # 數字最遠追到第幾段，決定圖要掛多久
    unit = str(v.get("unit", "")).strip()[:4]
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
            if not _traceable(val, window, unit):
                return None          # 一個對不上就整條丟掉，不留半真半假的圖
            for j in range(lo, hi):  # 記下它出自哪一段
                if _traceable(val, segments[j].get("text", ""), unit):
                    reach = max(reach, j)
            values.append((str(it.get("label", "")).strip()[:8], val))
        if not values:
            return None
        # 兩個以上的數字要並排比較，沒有標籤就只是兩根無意義的柱子
        if len(values) >= 2 and not all(lb for lb, _ in values):
            return None

    labels = [str(x).strip()[:10] for x in (v.get("steps") or []) if str(x).strip()]
    if kind == "steps" and len(labels) < 2:
        return None

    start = float(seg["start"])
    # 對比的後半句還沒講完就把圖收掉會很怪，掛到數字追得到的最後一段
    end = min(float(segments[reach]["end"]), start + HOLD_SEC + GROW_SEC)
    if end - start < 0.6:
        end = start + 0.6

    return {"seg": i, "type": kind, "start": round(start, 3), "end": round(end, 3),
            "title": str(v.get("title", "")).strip()[:14],
            "unit": unit,
            "values": values, "steps": labels,
            "source": text[:40]}


def has_judgment() -> bool:
    """規則包裡有沒有這個功能的判準。用來決定要不要跟使用者提一句。

    只看檔案在不在，**不要試著渲染**。渲染會把「prompt 多了一個變數而
    呼叫端沒帶」誤判成「沒有 Pro」——付了錢的訂閱者會被叫去買 Pro。
    """
    from .. import rules as _rules
    try:
        return _rules.load().has_prompt("visuals")
    except Exception:
        return True          # 判斷不了就別擋，讓 pick() 自己去試


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
    # 模板目錄要先備好，因為它會被寫進 prompt——順序反了就 UnboundLocalError，
    # 而外層的 try/except 會把它吞成「略過」，看起來像沒有 Pro 而不是壞掉。
    from . import webfx as _webfx
    tpls = _webfx.templates() if _webfx.available() else {}
    # 判準住在 Pro 規則包。免費包沒有這份 prompt——這個功能整個屬於 Pro。
    # 回空清單而不是丟例外：短影音的其他部分（字幕、字卡、封面）照做，
    # 只是少了圖。呼叫端負責告訴使用者為什麼少。
    pack = _rules.load()
    if not pack.has_prompt("visuals"):
        return []
    # 判準在卻渲染失敗＝程式問題，讓它炸上去給呼叫端報成「程式問題」，
    # 不要吞成空清單，否則付了錢的人會以為功能只是沒挑到東西。
    prompt = pack.prompt(
        "visuals", count=len(segments), numbered=numbered, max_n=max_n,
        types="、".join(SUPPORTED),
        # 沒有瀏覽器時目錄是空的，判斷腦就不會挑 fx——自動降級成 ASS 圖表
        fx_catalog=_webfx.catalog() if tpls else "（這台機器上不能用）")

    say(52, "判斷腦挑要配圖的句子…")
    try:
        data = llm.complete_json(prompt, tier=FAST)
    except LLMUnavailable:
        return []

    out, used, downgraded = [], set(), 0
    for v in (data.get("visuals") or []):
        if not isinstance(v, dict):
            continue
        is_fx = str(v.get("type", "")).strip() == FX
        # ASS 陽春圖表**一律不採用**，沒有例外。
        #
        # ASS 那套（純色矩形、描邊字）實測燒進成片比不放還差——熊董的原話是
        # 「特效變得意義不明，這樣不如乾脆不用」。所以寧可少配幾個圖，
        # 也不要用陽春圖表充數。
        #
        # ⚠️ 這裡原本寫成 `if tpls and not is_fx`，只在**有模板時**才擋。
        # 於是模板數為 0 的時候（規則包沒有 fx/），被判死刑的陽春圖表反而是
        # 唯一會出貨的東西——正好跟本意相反。沒有模板就不要配圖。
        if not is_fx:
            downgraded += 1
            continue
        spec = (_verify_fx(v, segments, tpls) if is_fx
                else _verify(v, segments))
        if not spec or spec["seg"] in used:
            continue
        used.add(spec["seg"])
        out.append(spec)
        if len(out) >= max_n:
            break

    # 前一張還沒收、後一張就進來的話要處理，但**不是留空檔**。
    #
    # 原本強制留 0.25 秒空白——那不是轉場，那是停頓，觀眾感受到的是
    # 「消失，然後出現」。讓前一張的退場（0.26s）跟後一張的進場（0.34s）
    # 交疊 0.15 秒，兩者在中段接住，變成溶接而不是斷點。
    out.sort(key=lambda v: v["start"])
    for a, b in zip(out, out[1:]):
        if a["end"] > b["start"] + OVERLAP_SEC:
            a["end"] = max(a["start"] + 0.8, b["start"] + OVERLAP_SEC)
    if downgraded:
        say(55, f"略過 {downgraded} 個只能用陽春圖表呈現的（寧可不配，也不要降低質感）")
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
    bw, gap, hmax = 118, 46, 250
    total = n * bw + (n - 1) * gap
    x0 = cx - total // 2
    # 基準線要往下推到最高的柱子＋數字都低於 CHART_TOP，否則會壓在標題上。
    # 舊值 y+hmax//2 讓柱頂正好落在 y-150 ＝ 標題的位置，實測「團隊規模對比」
    # 被柱子整個蓋掉。最高的元素是柱頂上方 34px 的數字（fs50，半高＋描邊約 29）。
    base = y + hmax - CHART_TOP_PAD + 34 + 29
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
    # 百分比數字畫在條的上方 60px（fs104，半高＋描邊約 56），照 CHART_TOP_PAD 反推
    x0, base = cx - W // 2, y - CHART_TOP_PAD + 56 + 60
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
    # 圖整體往下讓開標題：折線的最高點落在 base-H，末端數字又在那再上方 56px
    # （fs54，半高＋描邊約 31）。照 CHART_TOP_PAD 反推基準線，不要憑感覺加偏移。
    W, H = 760, 230
    x0, base = cx - W // 2, y - CHART_TOP_PAD + 31 + 56 + H
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
