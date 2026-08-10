# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""依「要保留的區間」把毛片剪接成淨毛片。

作法：`filter_complex` 對每個保留段做 trim/atrim + setpts/asetpts，再 concat 接起來，
單次 re-encode → 逐格精準。

filter 內容寫進暫存檔、用 `-filter_complex_script` 餵，避開 Windows 命令列長度上限
（保留段一多，filter 字串輕易上萬字元）。

## 兩條路：單次 filter_complex vs 切檔再接

上面那條路有一個前提：**保留區間必須照來源時間由前往後**。順剪永遠成立，
但精華短片的冷開場不成立——teaser 是把本體裡「最爆那句」複製到片頭，
所以第一段的來源時間可能遠在第二段之後。

filtergraph 的 concat 必須照順序輸出：要先吐 teaser，ffmpeg 就得先解碼到那裡，
而這段期間本體分支產出的每一格都堆在 graph 裡等人收。金句在後段是常態，
堆上百秒的解碼幀就是 MemoryError（AutoCut 2026-08-09 實測同一支片：只留單區間
rc=0，加上 teaser 就爆；距離 20s/24s 都過得去，143s 必爆）。

所以區間不是遞增時，改走 `_cut_by_segments()`：每段用 `-ss/-t` 各自切成檔案，
再用 concat demuxer 串流複製接起來。各段獨立編碼，完全不需要跨區間緩衝，
而且 `-ss` 讓每段直接跳到位置，不必為了後面的段從頭解碼一次。
"""

import gc
import os
import shutil
import tempfile
import time
from typing import List, Optional

from . import media
from .rules import load as load_rules

# 每段音訊頭尾的極短淡化長度。
#
# 為什麼需要：硬接（concat）兩段不連續的音訊時，接縫處波形會突然跳階，產生「啪」的
# 暫態雜訊——whisper 甚至會把它辨識成一個假音節（實測辨成「光」）。
# 頭尾微淡化讓波形從 0 起、回到 0 收，接縫就乾淨無爆音。
# 12ms 短到人耳聽不出來，只消爆音、不傷字音。
AFADE_SEC = 0.025


def _build_filter(keep: List[dict], afade_sec: float = AFADE_SEC) -> str:
    parts, labels = [], []
    for i, k in enumerate(keep):
        s, e = k["start"], k["end"]
        dur = e - s
        parts.append(f"[0:v]trim=start={s}:end={e},setpts=PTS-STARTPTS[v{i}];")

        af = (f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS,"
              + _afade_args(dur, afade_sec))
        parts.append(af + f"[a{i}];")
        labels.append(f"[v{i}][a{i}]")

    # concat 後把畫面轉成固定 30fps（CFR）。
    # 原片常是變動幀率（VFR，實際可能只有 ~22fps），不轉的話下游做「慢慢拉近」這類
    # 運鏡時，每格間隔不平均會看起來卡頓。轉 CFR 讓增量平均、動作滑順。
    parts.append("".join(labels)
                 + f"concat=n={len(keep)}:v=1:a=1[outvc][outa];[outvc]fps=30[outv]")
    return "\n".join(parts)


def _afade_args(dur: float, afade_sec: float = AFADE_SEC) -> str:
    """單段音訊的頭尾微淡化（理由見 AFADE_SEC）。太短的段不加，會整段被淡掉。"""
    if dur <= afade_sec * 3:
        return "anull"
    return (f"afade=t=in:st=0:d={afade_sec}"
            f",afade=t=out:st={round(dur - afade_sec, 4)}:d={afade_sec}")


def _is_ascending(keep: List[dict]) -> bool:
    """保留區間是否照來源時間由前往後。不是的話不能走單次 filter_complex（見模組說明）。"""
    return all(keep[i]["start"] >= keep[i - 1]["start"] for i in range(1, len(keep)))


def _free_models() -> None:
    """主動釋放 ASR 模型，把記憶體還給 ffmpeg。

    ffmpeg 在記憶體壓力下可能秒死，或編完在收尾階段 OOM（rc=-12 / 4294967284）。
    真凶是同一個程序還握著 ASR 模型（whisper ct2 約 3GB + Paraformer 約 1GB），
    讓系統 RAM 不夠 ffmpeg 收尾。

    **單純 gc.collect() 收不掉**——那些是還有活引用的模型快取，必須先主動清掉字典，
    gc 才有東西可回收。下一支影片或回測要用時會惰性重載。
    """
    for mod, attr in (("bearcut.asr.paraformer", "_MODEL"),
                      ("bearcut.asr.whisper", "_CACHE")):
        try:
            import importlib
            getattr(importlib.import_module(mod), attr).clear()
        except Exception:
            pass
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _run_with_retry(args: List[str], report, what: str) -> None:
    """跑 ffmpeg，失敗就釋放模型記憶體再試一次（理由見 `_free_models`）；再失敗就丟錯。"""
    out = media.ffmpeg(args)
    if out.returncode != 0:
        report(96, f"{what}失敗（rc={out.returncode}），釋放模型記憶體後重試一次…")
        _free_models()
        time.sleep(3)
        out = media.ffmpeg(args)
    if out.returncode != 0:
        tail = (out.stderr or "").strip().splitlines()[-15:]
        raise RuntimeError(f"{what}失敗（rc={out.returncode}）：\n" + "\n".join(tail))


def _cut_by_segments(input_path: str, keep: List[dict], output_path: str,
                     crf: int, preset: str, report) -> str:
    """每段各自切成檔案，再用 concat demuxer 接起來（何時走這條路見模組說明）。

    比單次 filter_complex 多開了幾次 ffmpeg，但總編碼量一樣（同樣那些秒數），
    而且 `-ss` 讓每段直接跳到位置，省掉「為了後面的段從頭解碼一次」。
    """
    from . import longform  # 只有這條路要用，放函式內避免模組層互相牽扯

    workdir = tempfile.mkdtemp(prefix="bearcut_seg_")
    segs: List[str] = []
    try:
        for i, k in enumerate(keep):
            s, e = float(k["start"]), float(k["end"])
            dur = round(e - s, 3)
            if dur <= 0:
                continue
            seg = os.path.join(workdir, f"seg{i:03d}.mp4")
            what = f"切第 {len(segs) + 1}/{len(keep)} 段"
            report(96, f"{what}（{dur:.1f}s）…")
            _run_with_retry([
                "-ss", f"{s:.3f}", "-i", input_path, "-t", f"{dur:.3f}",
                # fps=30 與編碼參數都跟單次 filter_complex 那條路一致：
                # 各段規格一致，concat 才能走串流複製（無損、最快）。
                "-vf", "fps=30", "-af", _afade_args(dur),
                "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-c:a", "aac", "-b:a", "192k",
                seg,
            ], report, what)
            segs.append(seg)

        if not segs:
            raise RuntimeError("保留區間長度都是 0，沒有東西可以剪。")
        report(98, f"接合 {len(segs)} 段…")
        longform.concat(segs, output_path)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return output_path


def cut_video(input_path: str, keep: List[dict], output_path: str,
              crf: Optional[int] = None, preset: Optional[str] = None,
              progress_cb=None) -> str:
    """依保留區間剪接影片。回輸出路徑。"""
    def report(p, msg):
        if progress_cb:
            progress_cb(p, msg)

    if not keep:
        raise RuntimeError(
            "保留區間是空的——全片都被判定要剪掉。\n"
            "這通常代表偵測參數太兇，請放寬 rulepack 的靜音或重複偵測門檻。")

    c = load_rules().section("cut")
    crf = crf if crf is not None else c.get("crf", 18)
    preset = preset or c.get("preset", "veryfast")

    # 沒有任何要剪的 → 直接複製，省一次編碼與畫質損耗
    if len(keep) == 1 and keep[0]["start"] <= 0.05:
        report(96, "沒有偵測到要剪的片段，直接輸出原片")
        shutil.copyfile(input_path, output_path)
        return output_path

    # 區間會往回跳（精華短片的冷開場 teaser）→ 單次 filter_complex 會爆記憶體，
    # 改切檔再接。判斷與理由見模組說明。
    if not _is_ascending(keep):
        report(96, f"保留區間有回頭（冷開場）→ 改切檔再接（{len(keep)} 段）…")
        _cut_by_segments(input_path, keep, output_path, crf, preset, report)
        report(99, f"淨毛片已輸出：{os.path.basename(output_path)}")
        return output_path

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                      encoding="utf-8")
    try:
        tmp.write(_build_filter(keep))
        tmp.close()

        args = [
            "-y", "-i", input_path,
            *media.filter_script_args(tmp.name),
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-r", "30",
            "-c:a", "aac", "-b:a", "192k",
            output_path,
        ]
        report(96, f"剪接中（{len(keep)} 個保留片段）…")
        _run_with_retry(args, report, "影片剪接")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    report(99, f"淨毛片已輸出：{os.path.basename(output_path)}")
    return output_path
