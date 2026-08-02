# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""協調器 —— 把各層串起來跑完一支影片。

**想理解整個系統，從這裡開始讀。** 每一段註解說明的是那一層的取捨，
不是它做了什麼（做了什麼看函式名就知道）。

## 順序不能亂

1. **辨識** whisper 給文字、Paraformer 給時間
2. **校字** 先修聽錯的字，**再**拿乾淨文字去判斷要剪哪裡
   —— 順序反了會把聽錯的字當成廢段刪掉
3. **偵測** 各層獨立產出「要剪的區間」
4. **安全閥** 只套在會產生幻覺的層
5. **對齊字邊界** 所有刀都用 Paraformer 字級時間校正，不削字
6. **合併 → 保留區間 → 剪**

## 沒有判斷腦也要能跑
判斷腦缺席時只降級成「只剪靜音 + 卡頓」，不是失敗。
"""

import os
from typing import Callable, Optional

from . import correct, cut as _cut, srtlint as _lint, subtitle as _sub
from . import media, plan as _plan, retest as _retest, syncqa as _syncqa
from . import verify as _verify, visualqa as _vqa
from .asr import paraformer, whisper
from .detect import fragments, gaps, redundant, restarts, safety, silence
from .llm import get_llm
from .rules import load as load_rules

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm")


def _out_paths(input_path: str, output_dir: Optional[str] = None) -> dict:
    d = output_dir or os.path.dirname(os.path.abspath(input_path))
    base = os.path.splitext(os.path.basename(input_path))[0]
    os.makedirs(d, exist_ok=True)
    return {
        "output": os.path.join(d, f"{base}_淨毛片.mp4"),
        "plan": os.path.join(d, f"{base}_待剪清單.json"),
        "review": os.path.join(d, f"{base}_剪輯判斷對照.txt"),
        "voided": os.path.join(d, f"{base}_暴走作廢.json"),
        "v1": os.path.join(d, f"{base}_字幕校對_V1.txt"),
        "srt": os.path.join(d, f"{base}_字幕.srt"),
        "verify_debug": os.path.join(d, f"{base}_剪前檢查.json"),
        "qa": os.path.join(d, f"{base}_品質報告.txt"),
        "frames": os.path.join(d, f"{base}_接縫畫面"),
    }


def _read_script(input_path: str) -> Optional[str]:
    """讀同名 .txt 腳本。沒有就回 None。"""
    p = os.path.splitext(input_path)[0] + ".txt"
    if os.path.exists(p):
        try:
            t = open(p, encoding="utf-8").read().strip()
            return t or None
        except OSError:
            return None
    return None


def analyze(input_path: str, do_cut: bool = True,
            output_dir: Optional[str] = None,
            profile: Optional[str] = None,
            progress_cb: Optional[Callable[[int, str], None]] = None) -> dict:
    """跑完一支影片：辨識 → 偵測 → 剪。回各項產出的路徑與計畫。

    `profile` 是「效率／平衡／精準」那一組預設，見 rulepack 的 profiles。
    """
    def report(p, msg):
        if progress_cb:
            progress_cb(p, msg)

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"找不到影片：{input_path}")

    rules = load_rules(refresh=bool(profile), profile=profile)
    paths = _out_paths(input_path, output_dir)
    duration = media.get_duration(input_path)
    mode = rules.get("_active_profile", "balanced")
    report(2, f"影片長度 {duration:.1f} 秒　模式：{mode}")

    # ── 1. whisper 辨識（文字內容） ──────────────────────────────────
    segments = whisper.transcribe_words(
        input_path,
        language=rules.get("asr.language", "zh"),
        model_size=rules.get("asr.model_size", "large-v3"),
        initial_prompt=rules.get("asr.initial_prompt"),
        progress_cb=progress_cb,
    )
    if not segments:
        raise RuntimeError(
            "沒有辨識到任何語音，無法判斷要剪哪裡。\n"
            "請確認這支影片有聲音，而且語言設定正確（目前是 "
            f"{rules.get('asr.language', 'zh')}）。")

    # 腳本只是參考——真實拍片幾乎不會有逐字稿，沒腳本一樣要能跑。
    # 但「有腳本卻沒配對到」是品質事故：曾有整批毛片因為沒改名而裸跑，
    # 校字沒參考稿、結尾 CTA 被當廢話剪掉。所以大聲警告，不要默默裸跑。
    script_text = _read_script(input_path)
    if not script_text:
        report(88, "⚠ 找不到同名腳本 .txt —— 本支裸跑：校字沒有參考稿。"
                   "若其實有腳本，請改成與影片同名放同資料夾再跑。")

    llm = get_llm()
    if not llm.available():
        report(88, f"⚠ {llm.describe()} —— 只會剪靜音與卡頓，"
                   "語意判斷（重講／NG／廢段）停用。")

    # ── 1.5 校字：一定要在判斷之前 ──────────────────────────────────
    # whisper 會把「一人公司」聽成「藝人公司」。拿沒校正的文字去判斷，
    # 判斷腦會把「讀不通」的句子當廢段刪掉——但講者其實講得很好。
    # 實測 R09：少了這一步就少抓 4 段重複。
    repl, hotwords = correct.load_replacements()
    segments = correct.apply_replacements(segments, repl, progress_cb)
    if llm.available():
        segments, suspects = correct.fix_typos(
            segments, llm, script_text=script_text, hotwords=hotwords,
            progress_cb=progress_cb)
        if suspects and rules.get("llm.suspect_escalation", True):
            segments, suspects = correct.escalate_suspects(
                segments, suspects, llm, script_text=script_text,
                progress_cb=progress_cb)
        for s in suspects[:5]:
            report(89, f"校字提醒（請人工確認）：{s}")

    # ── 2. 確定性偵測（不需判斷腦，最可靠） ──────────────────────────
    report(90, "偵測靜音與空白中…")
    sil = silence.silence_cuts(input_path, duration)

    gap_cuts = gaps.drop_overlapping(gaps.detect_gap_cuts(segments, duration), sil)
    if gap_cuts:
        report(90, f"偵測到 {len(gap_cuts)} 處字間卡頓")

    all_cuts = list(sil) + list(gap_cuts)

    # ── 3. Paraformer 字級時間（切點時間的唯一來源） ────────────────
    chars = []
    try:
        chars = paraformer.transcribe_chars(input_path, progress_cb=progress_cb)
        report(91, f"字級辨識完成，共 {len(chars)} 字")
    except Exception as e:
        # 字級辨識失敗不該讓整支片跑不完——降級成「只用確定性層 + 不做字邊界對齊」。
        report(91, f"⚠ 字級辨識失敗，重講偵測與字邊界對齊停用：{e}")

    # ── 4. 重講偵測（確定性候選 + 判斷腦裁決模糊的） ────────────────
    if chars:
        cands = restarts.find_restart_candidates(chars)
        confident = restarts.candidates_to_cuts(cands, confident_only=True)
        fuzzy = [c for c in cands if not c.get("confident")]
        if confident or fuzzy:
            report(92, f"重講候選 {len(cands)} 處（確定 {len(confident)}、"
                       f"待判斷 {len(fuzzy)}）")
        # 這一層會誤判 → 要套安全閥
        confident = safety.guard(confident, duration, "確定性重講層",
                                 voided_path=paths["voided"], report=report)
        all_cuts += confident

        # 破碎片段層：字級碎屑（口吃疊字、沒講完的半句、重錄殘字）。
        # 這些只存在於 Paraformer 原始字流，段落級判斷看不到——實測一支 98 秒口播，
        # 真正該剪的「重複」全是這種 0.1~0.3 秒的東西。
        # 給乾淨稿當參照，避免把「聽錯但其實是好內容」的字當碎屑剪掉。
        if llm.available():
            lines = paraformer.chars_to_lines(
                chars,
                gap_split=rules.get("asr.chars_gap_split", 0.25),
                max_chars=rules.get("asr.chars_max_len", 14))
            clean_ref = "\n".join(s_["text"] for s_ in segments)
            frag_cuts = fragments.detect_broken_fragments(
                lines, chars, llm, clean_ref=clean_ref, progress_cb=progress_cb)
            # 這層最容易暴走（碎屑很多、很短），門檻設更嚴
            frag_cuts = safety.guard(frag_cuts, duration, "破碎片段層",
                                     voided_path=paths["voided"],
                                     fragments=True, report=report)
            all_cuts += frag_cuts

    # ── 5. 判斷腦：整段重複 / NG take / 廢段 ────────────────────────
    if llm.available():
        try:
            drops = redundant.detect_redundant_segments(
                segments, llm, script_text=script_text, progress_cb=progress_cb)
            seg_cuts = [{"start": segments[d["index"]]["start"],
                         "end": segments[d["index"]]["end"],
                         "type": d.get("kind") or "repeat",
                         "reason": d["reason"]} for d in drops]
            seg_cuts = safety.guard(seg_cuts, duration, "判斷層",
                                    voided_path=paths["voided"], report=report)
            all_cuts += seg_cuts
        except Exception as e:
            report(92, f"⚠ 語意判斷略過，只用機械偵測：{e}")

        # 確定性的相鄰整段重複（不靠判斷腦，100% 可重現）
        adj = redundant.detect_adjacent_repeats(segments)
        if adj:
            adj_cuts = [{"start": segments[d["index"]]["start"],
                         "end": segments[d["index"]]["end"],
                         "type": "repeat", "reason": d["reason"]} for d in adj]
            all_cuts += safety.guard(adj_cuts, duration, "相鄰重複層",
                                     voided_path=paths["voided"], report=report)

    # ── 6. 對齊字邊界：不削字 ───────────────────────────────────────
    # 所有刀都要過這關。whisper 偶爾把字尾算太早，刀口會插進字中間把字削掉一半。
    if chars:
        before = len(all_cuts)
        all_cuts = paraformer.snap_cuts_off_chars(all_cuts, chars)
        if before != len(all_cuts):
            report(93, f"字邊界對齊：丟掉 {before - len(all_cuts)} 刀"
                       "（落在連續講話中的假空隙）")

    # ── 6.5 剪前自我修正：在真的剪下去之前先看會剪成什麼樣 ──────────
    # 剪完才發現接縫不通順就得重新編碼一次（很貴）。這一層先模擬、先修。
    # 雙向：既補漏剪的殘留，也還原剪過頭的內容——多剪比漏剪更難救，
    # 因為內容默默消失，使用者要看片才發現。
    if chars and llm.available() and rules.get("verify.self_verify", True):
        all_cuts = _verify.verify_and_repair(
            chars, all_cuts, llm, script_text=script_text,
            progress_cb=progress_cb, debug_path=paths["verify_debug"])

    # ── 7. 合併 → 保留區間 → 計畫 ──────────────────────────────────
    merged = _plan.merge_cuts(all_cuts, duration)
    keep = _plan.compute_keep(merged, duration)
    plan_data = _plan.build_plan(input_path, duration, all_cuts, merged, keep)

    _plan.write_plan_json(plan_data, paths["plan"])
    _plan.write_review_txt(plan_data, paths["review"])

    s = plan_data["summary"]
    report(95, f"計畫完成：{s['原長秒']}s → {s['剪後秒']}s（剪掉 {s['剪掉比例']}）")

    # ── 8. 字幕：時間軸對齊剪完之後的成片 ────────────────────────────
    mapped = _sub.map_segments(segments, keep)
    _sub.write_v1(mapped, paths["v1"],
                  title=os.path.splitext(os.path.basename(input_path))[0])
    _sub.write_srt(mapped, paths["srt"])
    report(95, f"字幕完成：{len(mapped)} 段（V1 校對檔 + SRT）")
    # 出廠檢查：只警告不擋流程——這些多半是辨識層的問題，字幕仍然可用
    _lint.report(paths["srt"], progress_cb=progress_cb)

    result = {"plan": paths["plan"], "review": paths["review"],
              "v1": paths["v1"], "srt": paths["srt"],
              "plan_data": plan_data}

    if do_cut:
        _cut.cut_video(input_path, keep, paths["output"], progress_cb=progress_cb)
        result["output"] = paths["output"]

        # ── 9. 剪後品質檢查 ─────────────────────────────────────────
        # 這些是「使用者一打開檔案就會發現、我們不看就不知道」的問題，
        # 所以必須自動驗。只回報不擋流程——成片已經在那裡了，
        # 攔下來只會讓使用者拿不到東西。
        qa_lines = []

        sync = _syncqa.check(paths["output"], srt=paths["srt"],
                             expected_sec=s["剪後秒"], progress_cb=progress_cb)
        qa_lines += [f"[音畫字] {x}" for x in sync]
        qa_lines += [f"[字幕] {x}" for x in _lint.lint(paths["srt"])]

        if rules.get("verify.output_retest", False):
            rt = _retest.retest_and_refix(
                paths["output"], keep, llm,
                max_rounds=rules.get("verify.retest_max_rounds", 1),
                model_size=rules.get("asr.model_size", "large-v3"),
                progress_cb=progress_cb)
            qa_lines += [f"[回測] {r['time']:.1f}s「{r['text'][:24]}」{r['reason']}"
                         for r in rt.get("residual", [])]
            result["retest"] = rt

        vq = _vqa.extract(paths["output"], keep, paths["frames"],
                          progress_cb=progress_cb)
        result["frames"] = vq

        _write_qa_report(paths["qa"], input_path, s, qa_lines, vq)
        result["qa"] = paths["qa"]
        result["qa_issues"] = qa_lines

    return result


def _write_qa_report(path: str, video: str, summary: dict,
                     issues: list, frames: dict) -> str:
    """寫品質報告。沒問題也要寫——「檢查過而且沒事」本身就是有價值的資訊。"""
    lines = [
        "品質檢查報告",
        "=" * 60,
        f"影片：{os.path.basename(video)}",
        f"原長 {summary['原長秒']}s → 剪後 {summary['剪後秒']}s（剪掉 {summary['剪掉比例']}）",
        "",
    ]
    if issues:
        lines += [f"發現 {len(issues)} 項需要注意：", "-" * 60]
        lines += [f"  · {x}" for x in issues]
        lines += ["", "以上都不影響檔案可用性，但建議確認。"]
    else:
        lines += ["✓ 所有自動檢查都通過，沒有發現問題。"]

    if frames.get("count"):
        lines += ["", "-" * 60,
                  f"接縫畫面已抽出 {frames['count']} 組 → {os.path.basename(frames['dir'])}/",
                  "可以逐張看接得順不順、人物有沒有瞬移。"]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def cut_from_plan(plan_path: str, output_dir: Optional[str] = None,
                  progress_cb: Optional[Callable[[int, str], None]] = None) -> str:
    """吃一份（可能被人工改過的）待剪清單，只執行剪片。

    這是「不同意就自己改」那條路的實作——使用者複核對照表後改 JSON，
    再用這個函式重剪，不必重跑整個辨識與判斷。
    """
    import json
    with open(plan_path, encoding="utf-8") as f:
        data = json.load(f)

    video = data.get("video")
    if not video or not os.path.exists(video):
        raise FileNotFoundError(
            f"待剪清單指向的影片不存在：{video}\n"
            "若影片移動過，請修改清單裡的 video 欄位。")

    keep = data.get("keep") or []
    if not keep:
        # 清單裡沒有 keep（人工只改了 cuts）→ 重算
        duration = data.get("duration_sec") or media.get_duration(video)
        keep = _plan.compute_keep(data.get("cuts") or [], duration)

    paths = _out_paths(video, output_dir)
    return _cut.cut_video(video, keep, paths["output"], progress_cb=progress_cb)
