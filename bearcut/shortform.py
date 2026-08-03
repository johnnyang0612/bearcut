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

import json
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
         theme: Optional[str] = None,
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
    # 頭頂在哪——決定卡片能放多大、放不放得下。先算，因為算圖時就要用。
    # 臉框的座標在「640 寬的取樣幀」空間（見 speaker.crop_box 的 scale），
    # 不是原片座標；直接除原片高度會算出偏高的位置。
    head_top = None
    if fx_list:
        try:
            faces = _speaker.detect_faces(video)
            if faces:
                from .visual.reframe import probe_size
                sw, sh = probe_size(video)
                k = (_vert.H / (640.0 * sh / sw)) if sw and sh else 1.0
                tops = sorted(f["y"] * k for f in faces)
                head_top = max(140.0, tops[len(tops) // 2] - 40)
                report(93, f"頭頂約在 y={head_top:.0f}"
                           f"（畫面 {head_top/_vert.H:.0%}），卡片放它上面")
        except Exception:
            pass

    # ⚠️ 順序：**先組好圖層再產字幕**。
    # 字幕與字卡要知道哪幾秒是整頁切走，而那件事寫在模板的 meta 裡，
    # 是組圖層時才附上去的。反過來做的話 build_ass 拿到的是還沒有 meta 的
    # 清單，區間永遠算成空的——字卡就會壓在動畫上。
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
                from .visual import inserts as _ins0
                # ── 版位的編輯邏輯 ────────────────────────────
                # upper：卡片縮到剛好放在頭上方的空白，**人留在畫面上**。
                #        一個數字沒必要佔掉整個版面。
                # full ：整頁切走，畫面只有素材、人不出現。要用讀的內容
                #        （多項清單、前後對比）才值得把畫面整個讓出來。
                # 沒有中間值——把人壓成一小條兩邊都不討好。
                z, y = 1.0, None
                if meta.get("placement") != "full":
                    fit = _ins0.fit_above_head(_vert.W, _vert.H, fx["w"],
                                               fx["h"], head_top,
                                               meta.get("min_zoom"))
                    if fit:
                        z, y = fit
                    else:
                        # 頭上方放不下（或縮到看不清楚），就整頁切走——不要硬塞。
                        # 密的模板寧可佔滿畫面，也不要縮成一團糊。
                        meta = dict(meta, placement="full")
                        report(93, f"動畫 {i}（{fx['template']}）"
                                   f"頭上方放不下，改成整頁切走")
                # 開場是 hook——觀眾要先看到人，前幾秒不整頁切走。
                # 一開場就是滿版圖表，滑手機的人不知道自己在看誰。
                if (meta.get("placement") == "full"
                        and float(fx["start"]) < _ins0.HOOK_SEC):
                    fit = _ins0.fit_above_head(_vert.W, _vert.H, fx["w"],
                                               fx["h"], head_top,
                                               meta.get("min_zoom"))
                    if fit:
                        meta = dict(meta, placement="upper")
                        z, y = fit
                        report(93, f"動畫 {i} 在開場 {fx['start']:.1f} 秒，"
                                   f"改壓在畫面上方（開場要先看到人）")
                    else:
                        report(93, f"動畫 {i} 在開場但放不下，整張略過")
                        continue
                if meta.get("placement") == "full":
                    z = _ins0.fit_full(_vert.W, _vert.H, fx["w"], fx["h"])
                # 算滿整個顯示時間，讓退場動畫也被算進去。
                # 只算進場那幾格再複製最後一格撐著的話，圖是突然消失的。
                span = max(1.2, float(fx["end"]) - float(fx["start"]))
                frames = _webfx.render_frames(
                    pathlib.Path(tpl["html"]).read_text(encoding="utf-8"),
                    fd, data=fx["fields"], w=fx["w"], h=fx["h"],
                    dur=span, zoom=z, theme=theme, progress_cb=None)
                if frames:
                    insert_layers.append({
                        "frames_pattern": os.path.join(fd, "f_%04d.png"),
                        "start": fx["start"], "end": fx["end"],
                        "w": int(round(fx["w"] * z)),
                        "h": int(round(fx["h"] * z)), "y": y, "meta": meta})
            except Exception as e:
                report(92, f"動畫 {i} 算圖略過（{e}）")
        if insert_layers:
            report(93, f"算好 {len(insert_layers)} 段動畫，準備插入畫面")

    # 字幕標色：判斷腦逐句挑要亮的詞。免費版單色、Pro 分三種語氣色，
    # 差別在規則包的判準，程式一樣。挑不到就退回內建的正規表示式。
    colour_plan = {}
    if subs:
        from .llm import get_llm as _get_llm
        from .visual import keywords as _kwmod
        try:
            _l = _get_llm()
            if _l.available():
                colour_plan = _kwmod.pick(subs, _l, progress_cb=progress_cb)
        except Exception as e:
            report(58, f"字幕標色略過（{e}），改用內建關鍵詞規則")
    if colour_plan:
        # 存成人工可改的檔——想換哪個詞亮、換什麼色，改這個檔重跑就好
        _p = os.path.join(d, f"{base}_字幕標色.json")
        with open(_p, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in sorted(colour_plan.items())},
                      f, ensure_ascii=False, indent=1)
        out["colour_plan"] = _p

    # 字卡讓位在這裡做，不是在 build_ass 裡面——那樣的話回傳的 card_list
    # 還是未過濾的，同步檢查會看到「示意圖與大字卡撞期」的假警報，
    # 而且 out["cards"] 報的張數也跟畫面上的對不起來。
    if insert_layers:
        _busy = [(float(v["start"]) - 0.2, float(v["end"]) + 0.2)
                 for v in insert_layers]
        kept = [c for c in card_list
                if not any(float(c["start"]) < e and float(c["end"]) > b
                           for b, e in _busy)]
        if len(kept) < len(card_list):
            report(94, f"大字卡讓位 {len(card_list) - len(kept)} 張"
                       f"（那幾秒畫面上是動畫）")
        card_list = kept

    _vert.build_ass(subs, out["ass"], title=title, cta=cta, visuals=ass_visuals,
                    stage_fx=insert_layers, card_list=card_list,
                    long_form=long_form, colour_plan=colour_plan)

    try:
        _vert.render(src, out["ass"], out["video"], FONTS, logo=logo,
                     insert_layers=insert_layers or None,
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

    # ── 可複核的計畫檔 ──────────────────────────────────────
    # 跟 _待剪清單.json 同一個用意：把「機器決定了什麼」攤開讓人看得懂、改得動。
    # 不想要某張圖、想換個模板、想改文字，改這個檔重跑就好，不用重新辨識。
    if visual_list or card_list:
        marks = {
            "$說明": "動態示意圖與大字卡的插入計畫。改完重跑就會照這份走。",
            "示意圖": [
                {"秒數": round(float(v["start"]), 2),
                 "到": round(float(v["end"]), 2),
                 "模板": v.get("template") or v.get("type"),
                 "版位": (v.get("meta") or {}).get("placement", "upper"),
                 "欄位": v.get("fields") or v.get("values") or v.get("steps"),
                 "依據原句": v.get("source", "")}
                for v in sorted(visual_list, key=lambda x: x["start"])],
            "大字卡": [
                {"秒數": round(float(c["start"]), 2),
                 "到": round(float(c["end"]), 2),
                 "上": c.get("top", ""), "主": c.get("key", "")}
                for c in sorted(card_list, key=lambda x: x["start"])],
        }
        p = os.path.join(d, f"{base}_視覺標記.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(marks, f, ensure_ascii=False, indent=1)
        out["marks"] = p

    # 同步 QA：字幕與畫面的時間軸有沒有對上。
    # 這屬於「剪完自己回測」的承諾，所以免費版也要有。
    try:
        qa = _sync_qa(subs, visual_list, card_list, out["video"])
        p = os.path.join(d, f"{base}_同步QA.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(qa)
        out["sync_qa"] = p
        bad = sum(1 for ln in qa.splitlines() if ln.startswith("  ⚠"))
        report(99, f"同步檢查：{'全部正常' if not bad else f'{bad} 項提醒'}")
    except Exception as e:
        report(99, f"同步檢查略過（{e}）")

    out["cards"] = card_list
    out["visuals"] = visual_list
    return out


def _sync_qa(subs, visuals, cards, video) -> str:
    """字幕與畫面的同步檢查。回一份人看得懂的報告。

    檢查的是「時間軸對不對」，不是「內容好不好」——後者要人看。
    """
    lines = ["BearCut 同步檢查", "=" * 40, ""]
    try:
        dur = media.get_duration(video)
    except Exception:
        dur = 0.0
    lines.append(f"成片長度　{dur:.2f} 秒")
    lines.append(f"字幕　　　{len(subs)} 句")
    lines.append(f"示意圖　　{len(visuals)} 個")
    lines.append(f"大字卡　　{len(cards)} 張")
    lines.append("")

    warn = []
    if subs:
        last = max(float(s["end"]) for s in subs)
        if dur and last > dur + 0.5:
            warn.append(f"⚠ 字幕最後一句到 {last:.2f} 秒，超出成片 {dur:.2f} 秒")
        # 字幕之間不該重疊
        for a, b in zip(subs, subs[1:]):
            if float(a["end"]) > float(b["start"]) + 0.05:
                warn.append(f"⚠ 字幕重疊：{a['end']:.2f} > {b['start']:.2f}"
                            f"（「{a['text'][:10]}」與「{b['text'][:10]}」）")
        # 太快的字幕看不完：一秒最多讀 9 個中文字。
        # 算的是**畫面上實際顯示的那一段**——長句會被拆成兩段依序顯示，
        # 拿整句的字數去除整句的秒數會誤報。
        from .subs import split_rows as _rows
        from .visual.style import TYPE as _T
        for s in subs:
            rows = _rows(s.get("text", ""), max_len=_T["sub_max_len"])
            n_chunk = max(1, (len(rows) + 1) // 2)
            per = (float(s["end"]) - float(s["start"])) / n_chunk
            n = len(s.get("text", "")) / n_chunk
            if per > 0 and n / per > 9:
                warn.append(f"⚠ 字幕太快：{s['start']:.2f} 秒的「{s['text'][:12]}」"
                            f"（每段約 {n:.0f} 字只有 {per:.2f} 秒）")

    for v in visuals:
        if dur and float(v["end"]) > dur + 0.3:
            warn.append(f"⚠ 示意圖超出片尾：{v['start']:.2f}~{v['end']:.2f} 秒")
        if float(v["end"]) - float(v["start"]) < 1.0:
            warn.append(f"⚠ 示意圖太短：{v['start']:.2f} 秒只掛 "
                        f"{float(v['end']) - float(v['start']):.2f} 秒")
    # 示意圖與大字卡不該同時在畫面上——兩者都在上半部
    for v in visuals:
        for c in cards:
            if float(v["start"]) < float(c["end"]) and \
               float(v["end"]) > float(c["start"]):
                warn.append(f"⚠ 示意圖與大字卡時間重疊："
                            f"{v['start']:.2f} 秒的圖 vs {c['start']:.2f} 秒的卡")

    if warn:
        lines.append(f"提醒 {len(warn)} 項：")
        lines += [f"  {w}" for w in warn]
    else:
        lines.append("沒有發現時間軸問題。")
    lines += ["", "（這份只檢查時間軸對不對，內容好不好還是要看片。）"]
    return "\n".join(lines) + "\n"
