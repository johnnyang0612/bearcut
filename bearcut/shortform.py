# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""短影音成片 —— 把剪好的片變成可以直接發的直式短片。

流程：字幕 → 挑金句做字卡 → （選用）追講者裁切 → 轉直式燒錄 → 封面

## 與順剪的分工

`pipeline.analyze()` 負責「剪掉不該留的」，這裡負責「把留下的包裝好」。
兩者分開的理由：剪輯是耗時的（辨識 + 判斷 + 編碼），而視覺包裝常常要重做幾次
（換標題、換字卡、調顏色）。分開就不必為了改一個字卡重跑整個辨識。
"""

import os
import pathlib
from typing import Callable, List, Optional

from . import media
from .env.platform import ROOT
from .rules import load as load_rules
from .srtlint import parse_srt
from .visual import cards as _cards
from .visual import cover as _cover
from .visual import speaker as _speaker
from .visual import vertical as _vert

FONTS = str(ROOT / "assets" / "fonts")


def _srt_to_subs(srt_path: str) -> List[dict]:
    """SRT → 內部字幕格式。"""
    def sec(ts):
        try:
            h, m, rest = ts.split(":")
            s, ms = rest.split(",")
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
        except (ValueError, AttributeError):
            return 0.0

    return [{"start": sec(c["start"]), "end": sec(c["end"]),
             "text": c["text"].replace("\n", "")}
            for c in parse_srt(srt_path)]


def make(video: str, srt: Optional[str] = None,
         title: Optional[str] = None, cta: Optional[str] = None,
         output_dir: Optional[str] = None,
         use_cards: bool = True, use_visuals: bool = True,
         follow_speaker: bool = False,
         logo: Optional[str] = None, make_cover: bool = True,
         progress_cb: Optional[Callable] = None) -> dict:
    """把一支剪好的片做成直式短影音。回產出的路徑。"""
    def report(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not os.path.exists(video):
        raise FileNotFoundError(f"找不到影片：{video}")

    d = output_dir or os.path.dirname(os.path.abspath(video))
    base = os.path.splitext(os.path.basename(video))[0].replace("_淨毛片", "")
    os.makedirs(d, exist_ok=True)
    out = {
        "video": os.path.join(d, f"{base}_直式.mp4"),
        "cover": os.path.join(d, f"{base}_封面.jpg"),
        "ass": os.path.join(d, f"{base}_直式.ass"),
    }

    # 字幕：沒指定就找同名的
    srt = srt or os.path.splitext(video)[0].replace("_淨毛片", "") + "_字幕.srt"
    subs = _srt_to_subs(srt) if os.path.exists(srt) else []
    if not subs:
        report(95, "⚠ 找不到字幕檔，直式短片將不含字幕。"
                   "先跑 bearcut cut 產生字幕會更完整。")

    # 大字卡
    card_list = []
    visual_list = []
    if use_cards and subs:
        from .llm import get_llm
        llm = get_llm()
        if llm.available():
            segs = [{"start": s["start"], "end": s["end"], "text": s["text"]}
                    for s in subs]
            try:
                dur = media.get_duration(video)
            except Exception:
                dur = 60.0
            card_list = _cards.pick(segs, llm, dur, progress_cb=progress_cb)

            # 動態示意圖：依語意配圖表。判準在規則包，數字必須追得回逐字稿。
            # 失敗不該讓整支短影音做不出來——沒有圖就只是少了圖。
            if use_visuals:
                from .visual import motion as _motion
                if not _motion.has_judgment():
                    # 講一句就好，不要每次都推銷——但也不要讓他以為功能壞了
                    report(56, "動態示意圖是 Pro 功能，這支略過（其餘照做）")
                else:
                    try:
                        visual_list = _motion.pick(segs, llm, progress_cb=progress_cb)
                        # 同一句不要既上字卡又上圖，會打架
                        taken = {v['seg'] for v in visual_list}
                        card_list = [c for i, c in enumerate(card_list)
                                     if i not in taken]
                    except Exception as e:
                        # 有 Pro 判準卻做不出來＝真的壞了，不是「沒買」。
                        # 講清楚是程式錯誤，否則使用者會以為是授權問題而去查訂閱。
                        import traceback
                        report(56, f'★ 動態示意圖出錯（程式問題，非授權問題）：'
                                   f'{type(e).__name__}: {e}')
                        for ln in traceback.format_exc().strip().splitlines()[-3:]:
                            report(56, f'    {ln.strip()}')

    # 追講者（選用）：偵測不到或多人就自動退回不裁切
    src = video
    if follow_speaker:
        faces = _speaker.detect_faces(video, progress_cb=progress_cb)
        sw, sh = _speaker.__dict__.get("_probe", lambda v: (0, 0))(video) \
            if False else (0, 0)
        from .visual.reframe import probe_size, render as reframe_render
        sw, sh = probe_size(video)
        box = _speaker.crop_box(faces, sw, sh) if faces else None
        if box:
            tmp = os.path.join(d, f"{base}_追講者.mp4")
            src = reframe_render(video, tmp, box, progress_cb=progress_cb)
            report(96, "已裁切到講者")
        else:
            report(96, "改用不裁切版面（雙人或偵測不到臉，避免漏人）")

    long_form = False
    try:
        long_form = media.get_duration(video) > 180
    except Exception:
        pass

    # fx（HTML 模板）不進 ASS——它是獨立的透明圖層，燒完字幕之後才疊上去
    fx_list = [v for v in visual_list if v.get("type") == "fx"]
    ass_visuals = [v for v in visual_list if v.get("type") != "fx"]
    # ⚠️ 順序：**先組好圖層再產字幕**。
    # 字幕與字卡要知道哪幾秒會換成舞台版面，而「是不是舞台版位」寫在模板的
    # meta 裡，是組圖層時才附上去的。反過來做的話 build_ass 拿到的是還沒有
    # meta 的清單，舞台區間永遠算成空的——字卡會壓在動畫上、字幕會被小窗蓋住。
    #
    # 動畫先算成 PNG 序列，跟字幕在同一次編碼裡插進去。
    # 分兩次做的話每次都是一輪有損轉檔，而且圖會蓋在字幕上。
    fx_dir = None
    insert_layers = []
    if fx_list:
        from .visual import webfx as _webfx
        import tempfile as _tf
        fx_dir = _tf.mkdtemp(prefix="bearcut-fx-")
        tpls = _webfx.templates()
        for i, fx in enumerate(fx_list, 1):
            tpl = tpls.get(fx["template"])
            if not tpl:
                continue
            try:
                fd = os.path.join(fx_dir, f"L{i}")
                os.makedirs(fd, exist_ok=True)
                meta = tpl.get("meta") or {}
                # 舞台版位的圖要撐滿舞台，模板寫的是設計尺寸
                from .visual import inserts as _ins0
                z = (_ins0.stage_zoom(_vert.W, _vert.H, fx["w"], fx["h"])
                     if meta.get("placement") == "stage" else 1.0)
                frames = _webfx.render_frames(
                    pathlib.Path(tpl["html"]).read_text(encoding="utf-8"),
                    fd, data=fx["fields"], w=fx["w"], h=fx["h"],
                    dur=fx["dur"], zoom=z, progress_cb=None)
                if frames:
                    insert_layers.append({
                        "frames_pattern": os.path.join(fd, "f_%04d.png"),
                        "start": fx["start"], "end": fx["end"],
                        "w": int(round(fx["w"] * z)),
                        "h": int(round(fx["h"] * z)), "meta": meta})
            except Exception as e:
                report(92, f"動畫 {i} 算圖略過（{e}）")
        if insert_layers:
            report(93, f"算好 {len(insert_layers)} 段動畫，準備插入畫面")

    _vert.build_ass(subs, out["ass"], title=title, cta=cta, visuals=ass_visuals,
                    stage_fx=insert_layers, card_list=card_list,
                    long_form=long_form)

    # 舞台版位要把人像縮進下方小窗——窗是寬扁的，隨便取一條會切掉額頭或下巴，
    # 所以用臉部偵測抓實際的高度。偵測不到就用經驗值，不會失敗。
    face_center = None
    if insert_layers:
        from .visual import inserts as _ins
        if _ins.stage_spans(insert_layers):
            if fx_dir:
                _ins.stage_assets(_vert.W, _vert.H, fx_dir)
            try:
                faces = _speaker.detect_faces(src)
                if faces:
                    from .visual.reframe import probe_size
                    sw, sh = probe_size(src)
                    # ⚠️ 臉框的座標在「640 寬的取樣幀」空間裡（見 speaker.crop_box
                    # 的 scale = src_w / 640），不是原片座標。直接除原片高度會
                    # 算出偏高的位置，小窗就裁到牆壁而不是臉。
                    sample_h = 640.0 * sh / sw if sw else sh
                    ys = sorted(f["y"] + f["h"] / 2 for f in faces)
                    face_center = ys[len(ys) // 2] / sample_h
                    face_center = min(0.85, max(0.15, face_center))
                    report(93, f"人臉在畫面 {face_center:.0%} 高度，小窗照這個裁")
            except Exception:
                pass

    try:
        _vert.render(src, out["ass"], out["video"], FONTS, logo=logo,
                     insert_layers=insert_layers or None,
                     face_center=face_center, assets_dir=fx_dir,
                     progress_cb=progress_cb)
    finally:
        if fx_dir:
            import shutil as _sh
            _sh.rmtree(fx_dir, ignore_errors=True)

    if make_cover:
        main = (card_list[0]["key"] if card_list else (title or base))[:12]
        sub = (card_list[0]["top"] if card_list else "")[:16]
        c = _cover.make(out["video"], out["cover"], main=main, sub=sub,
                        brand=load_rules().get("brand.name", ""))
        if c:
            report(99, f"封面已產出：{os.path.basename(c)}")
        else:
            out.pop("cover", None)

    out["cards"] = card_list
    out["visuals"] = visual_list
    return out
