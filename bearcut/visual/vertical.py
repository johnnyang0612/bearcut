# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""直式短影音版面 —— 把 16:9 的片變成 9:16，並燒上字幕與字卡。

## 三帶版面

    頂帶  0~390     標題 / 大字卡
    中帶  400~1008  影片（16:9 縮到寬 1080 = 高 608）
    下帶 1008~1650  CTA + 字幕

三帶互不重疊，且都避開平台 UI。**影片不裁切**——雙人對談裁了會漏人。
背景用同一支影片放大模糊填底，比純色底自然得多。

## 為什麼不預設裁切追講者

追講者滿版視覺上更好，但它會漏人：雙人對談時裁到 A，B 講話的反應就看不到了。
而且臉部偵測失敗時的退路必須存在。所以**預設 fit（不裁切、模糊填底）**，
追講者是選用的加強。
"""

import os
from typing import Callable, List, Optional

from .. import media
from ..subs import split_rows
from . import cards as _cards
from . import keywords as _kw
from .style import (ANCHORS, BANDS, H, PALETTE, SAFE, TYPE, W, clean_text,
                    esc, header, style_line, ts)

FG_TOP = BANDS["middle"][0]              # 影片上緣
FG_H = BANDS["middle"][1] - FG_TOP       # 影片高度


def _sub_styles() -> List[str]:
    return [
        style_line("Sub", TYPE["sub_size"], PALETTE["white"],
                   outline=TYPE["sub_outline"], shadow=TYPE["sub_shadow"],
                   align=2, margin_v=ANCHORS["sub_bottom_margin"]),
        style_line("Title", TYPE["title_size"], PALETTE["white"],
                   outline=TYPE["title_outline"], shadow=2.0, align=8, margin_v=0),
        style_line("CTA", TYPE["cta_size"], PALETTE["yellow"],
                   outline=5.5, shadow=2.0, align=8, margin_v=0),
    ]


def build_ass(subs: List[dict], ass_path: str,
              title: Optional[str] = None,
              cta: Optional[str] = None,
              card_list: Optional[List[dict]] = None,
              visuals: Optional[List[dict]] = None,
              stage_fx: Optional[List[dict]] = None,
              long_form: bool = False,
              keywords: Optional[List[str]] = None) -> str:
    """產直式 ASS：底部字幕（關鍵詞上色）+ 頂部標題/字卡 + 結尾 CTA。"""
    lines = header(W, H, _sub_styles() + _cards.styles())
    ev: List[str] = []
    cx = W // 2

    # 舞台版位的那幾秒，人像縮到下方小窗——字幕要跟著移到小窗上方，
    # 不然字會壓在人臉上。同時間的大字卡直接不畫，會跟動畫疊成一團。
    from . import inserts as _ins
    _spans = _ins.stage_spans(stage_fx or [])
    _stage_sub_y = _ins.stage_subtitle_y(H)

    def _on_stage(s0, s1):
        return any(s0 < e and s1 > b for b, e in _spans)

    # 字幕：每列最多 8 字（8×72px + 關鍵詞放大 118% 仍 < 700px 安全寬）
    for s in subs:
        text = clean_text(s.get("text", ""))
        if not text:
            continue
        rows = split_rows(text, max_len=TYPE["sub_max_len"])
        body = "\\N".join(_kw.decorate(r, keywords, long_form=long_form)
                          for r in rows[:2])
        place = (f"\\an5\\pos({cx},{_stage_sub_y})"
                 if _on_stage(s["start"], s["end"]) else "")
        ev.append(f"Dialogue: 0,{ts(s['start'])},{ts(s['end'])},Sub,,0,0,0,,"
                  f"{{{place}\\fad({TYPE['fade_ms']},{TYPE['fade_ms']})}}{body}")

    # 開場標題：前 3.5 秒，讓中途滑進來的人知道這支在講什麼。
    #
    # ⚠️ 字卡出現時標題要讓位。兩者都在頂帶、只差 80px，同時出現會被讀成同一段話
    # （實測「一人公司怎麼做到的」＋「一人公司十個月」黏成一句，很混亂）。
    #
    # 動態示意圖同樣在頂帶，而且畫得比字卡更高更大——實測開頭的計數器
    # 「1,000萬」直接壓在標題字上。所以三種都要讓位：
    #   card_list  大字卡
    #   visuals    ASS 圖表（沒有瀏覽器時的降級路線）
    #   stage_fx   HTML 模板動畫  ← 漏掉這個就是實測看到的那個 bug。
    #              `visuals` 參數是「非 fx」那一份，從裡面找 fx 永遠找不到。
    if title:
        t = clean_text(title)[:16]
        title_end = 3.5
        for c in (list(card_list or []) + list(visuals or [])
                  + list(stage_fx or [])):
            if c["start"] < title_end:
                title_end = min(title_end, c["start"] - 0.1)
        if title_end > 0.5:
            ev.append(f"Dialogue: 1,{ts(0)},{ts(title_end)},Title,,0,0,0,,"
                      f"{{\\pos({cx},{ANCHORS['title_y']})\\fad(200,200)}}{t}")

    # 大字卡。舞台時間內的要拿掉——那幾秒上半部整塊是動畫的，
    # 字卡疊上去會壓在卡片上。段號去重擋不掉這種「不同段但時間交疊」的情況。
    if card_list:
        ev += _cards.events(
            [c for c in card_list if not _on_stage(c["start"], c["end"])], W)

    # 動態示意圖跟字卡掛在同一區（畫面上半部），所以同一句不會兩者都上——
    # motion.pick() 已經把段號去重，這裡只負責把事件疊進去。
    if visuals:
        from . import motion as _motion
        ev += _motion.events(visuals, W)

    # 結尾 CTA：影片下緣與字幕之間的空檔，不壓字幕
    if cta and subs:
        end = subs[-1]["end"]
        ev.append(f"Dialogue: 1,{ts(max(0, end - 3.0))},{ts(end)},CTA,,0,0,0,,"
                  f"{{\\pos({cx},{ANCHORS['cta_y']})\\fad(200,150)}}{clean_text(cta)[:14]}")

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(lines + "\n".join(ev) + "\n")
    return ass_path


def _filter(ass_path: str, fonts_dir: str, logo: Optional[str] = None,
            already_portrait: bool = False,
            insert_layers: Optional[List[dict]] = None,
            face_center: Optional[float] = None,
            assets_dir: Optional[str] = None) -> str:
    """直式版面的 filter。

    來源比例不同，處理方式也不同：

    - **橫式來源**：縮到寬度 1080 放在中帶，背景用同一支影片放大模糊填底。
      **不裁切前景**——雙人對談裁了會漏人。
    - **直式來源**（手機直拍）：本來就是 9:16，直接縮放裁切填滿即可。
      再跑一次模糊合成是白費運算，而且前景會溢出中帶、蓋掉字卡區。
    """
    if already_portrait:
        base = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H}[base]")
    else:
        bg = (f"[0:v]scale={W}:-2,crop={W}:{H}:0:(ih-{H})/2,"
              f"boxblur=28:2,eq=brightness=-0.12[bg]")
        fg = f"[0:v]scale={W}:-2[fg]"
        base = f"{bg};{fg};[bg][fg]overlay=0:{FG_TOP}[base]"

    if logo and os.path.exists(logo):
        # LOGO 放右上角，避開頂帶字卡的置中區
        base = (f"{base};movie='{esc(logo)}',scale=140:-1[lg];"
                f"[base][lg]overlay={W - 180}:40[based];[based]null[base]")

    # 插入圖接在字幕**之前**：圖在下、字幕在上。
    # 反過來的話，整頁切走的圖會把字幕蓋掉——剪輯師切 B-roll 時字幕照樣在。
    src = "base"
    if insert_layers:
        from . import inserts as _ins
        frag, _ = _ins.build(insert_layers, W, H, base_label="base",
                             out_label="gfx", face_center=face_center,
                             assets_dir=assets_dir)
        if frag:
            base = f"{base};{frag}"
            src = "gfx"
    return (f"{base};[{src}]ass='{esc(ass_path)}':"
            f"fontsdir='{esc(fonts_dir)}'[outv]")


def render(video: str, ass_path: str, out_path: str, fonts_dir: str,
           logo: Optional[str] = None, crf: int = 19,
           insert_layers: Optional[List[dict]] = None,
           face_center: Optional[float] = None,
           assets_dir: Optional[str] = None,
           progress_cb: Optional[Callable] = None) -> str:
    """把橫式影片轉成直式並燒上字幕字卡。

    `insert_layers` 是要插進畫面的動畫（PNG 序列）。跟字幕在同一次編碼裡做完，
    不要事後再疊一輪——每疊一次就多一次有損轉檔。
    """
    def report(p, m):
        if progress_cb:
            progress_cb(p, m)

    # 來源已經是直式就不做模糊填底（見 _filter）
    portrait = False
    probe = media.ffprobe(["-select_streams", "v", "-show_entries",
                           "stream=width,height", "-of", "csv=p=0", video])
    try:
        w_, h_ = (int(x) for x in (probe.stdout or "").strip().split(",")[:2])
        portrait = h_ >= w_
    except (ValueError, TypeError):
        pass
    report(97, "來源為直式，直接填滿" if portrait else "來源為橫式，模糊填底")

    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                      encoding="utf-8")
    try:
        tmp.write(_filter(ass_path, fonts_dir, logo, already_portrait=portrait,
                          insert_layers=insert_layers, face_center=face_center,
                          assets_dir=assets_dir))
        tmp.close()
        extra: List[str] = []
        if insert_layers:
            from . import inserts as _ins
            _, extra = _ins.build(insert_layers, W, H, face_center=face_center,
                                  assets_dir=assets_dir)
            report(97, f"轉直式，燒字幕並插入 {len(insert_layers)} 段動畫…")
        else:
            report(97, "轉直式並燒錄字幕字卡…")
        # 長度封頂。合成裡只要有一個不會自己結束的來源（靜態圖、color），
        # 輸出就可能一路編下去——這個坑踩過兩次（709MB、1.77GB），
        # 所以不管有沒有插入圖層，一律用來源長度封住。
        cap: List[str] = []
        try:
            d = float(media.get_duration(video))
            if d > 0:
                cap = ["-t", f"{d:.3f}"]
        except Exception:
            pass
        r = media.ffmpeg(["-y", "-i", video, *extra,
                          *media.filter_script_args(tmp.name),
                          *cap, "-map", "[outv]", "-map", "0:a?",
                          "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                          "-pix_fmt", "yuv420p",
                          "-c:a", "aac", "-b:a", "192k", out_path])
        if r.returncode != 0:
            tail = (r.stderr or "").strip().splitlines()[-12:]
            raise RuntimeError("直式轉檔失敗：\n" + "\n".join(tail))
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    report(99, f"直式短片已輸出：{os.path.basename(out_path)}")
    return out_path
