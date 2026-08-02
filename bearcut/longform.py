# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""長片：多檔合成一支。

## 為什麼不是「把長片直接丟進 pipeline」

Podcast、講座這類素材通常本來就是分段錄的（Part1…PartN），而且很長。
直接把一小時的片丟進辨識與判斷腦有兩個問題：

- **辨識記憶體**：字級時間戳要把整支片的詞都留在記憶體裡
- **判斷腦會失焦**：一次讀一小時的逐字稿，重講與廢段的判斷品質明顯下降

所以架構是**各段各自順剪，再無縫接成一支**。每段都短、判斷腦不踩超長片，
接縫由 ffmpeg 處理。這也剛好對上素材原本就是分段的事實。

## 接合策略

- **各段解析度一致** → concat demuxer 串流複製（`-c copy`），零畫質損耗、最快。
  各段都出自同一支 `cut.cut_video`（同一組編碼參數），一致是常態。
- **不一致** → 退回重編，統一縮放補邊到第一支的解析度。
  這條路慢很多，但總比接不起來好。

## 字幕

各段的 SRT 時間軸都是從 0 開始的，接成一支之後全部錯位。
`merge_srts()` 依「前面各段**淨毛片**的累積時長」位移——注意是剪完的長度，
不是原始長度，因為接起來的是剪完的片。
"""

import glob
import os
import re
import shutil
import subprocess
import tempfile
from typing import Callable, List, Optional, Tuple

from . import media as _media

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".wmv", ".flv", ".webm")

# BearCut 自己的產出，掃資料夾時要跳過，否則會把剪好的片再剪一次。
# 跟 pipeline._out_paths() 的命名對齊——那邊改了這裡也要改。
_ARTIFACT_MARKS = ("_淨毛片", "_成片_直式", "_完整淨毛片", "_精華_", "_封面")


def _is_artifact(path: str) -> bool:
    name = os.path.splitext(os.path.basename(path))[0]
    return any(m in name for m in _ARTIFACT_MARKS)


def _natural_key(name: str):
    """檔名自然排序：讓 Part2 排在 Part10 前面。

    純字典序會把 Part10 排到 Part2 前面，接出來的片段順序就錯了——
    而且錯得很安靜，要看完才會發現。
    """
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def find_parts(folder: str) -> List[str]:
    """掃資料夾裡的原始影片（排除本系統的產出），依檔名自然排序。"""
    files = []
    for ext in VIDEO_EXTS:
        files.extend(glob.glob(os.path.join(folder, f"*{ext}")))
        files.extend(glob.glob(os.path.join(folder, f"*{ext.upper()}")))
    parts = [f for f in set(files) if not _is_artifact(f)]
    return sorted(parts, key=lambda f: _natural_key(os.path.basename(f)))


def _probe_wh(path: str) -> Optional[Tuple[int, int]]:
    out = _media.ffprobe(["-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0:s=x", path])
    try:
        w, h = (out.stdout or "").strip().split("x")
        return int(w), int(h)
    except (ValueError, AttributeError):
        return None


def _srt_ts_to_sec(ts: str) -> float:
    hh, mm, rest = ts.strip().split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def _sec_to_srt_ts(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def merge_srts(pairs: List[Tuple[str, str]], out_srt: str) -> Optional[str]:
    """把各段 SRT 依前面各段淨毛片的累積時長位移，接成對齊完整片的一份。

    `pairs` = [(該段淨毛片路徑, 該段 SRT 路徑), …]，順序就是接片順序。
    全部都沒有 SRT 就回 None——那不是錯誤，只是沒字幕可合。
    """
    offset, idx, blocks, any_srt = 0.0, 1, [], False
    for clean, srt in pairs:
        if srt and os.path.isfile(srt):
            any_srt = True
            try:
                raw = open(srt, "r", encoding="utf-8").read().strip()
            except OSError:
                raw = ""
            for blk in re.split(r"\n\s*\n", raw):
                lines = [ln for ln in blk.splitlines() if ln.strip()]
                if len(lines) < 2:
                    continue
                tline = next((ln for ln in lines if "-->" in ln), None)
                if not tline:
                    continue
                start, end = [t.strip() for t in tline.split("-->")]
                text = "\n".join(lines[lines.index(tline) + 1:]) or ""
                blocks.append(f"{idx}\n{_sec_to_srt_ts(_srt_ts_to_sec(start) + offset)} --> "
                              f"{_sec_to_srt_ts(_srt_ts_to_sec(end) + offset)}\n{text}")
                idx += 1
        # 位移用的是**剪完**的長度：接起來的是淨毛片，不是原始素材
        try:
            offset += _media.get_duration(clean)
        except Exception:
            pass
    if not any_srt:
        return None
    with open(out_srt, "w", encoding="utf-8") as f:
        f.write("\n\n".join(blocks) + "\n")
    return out_srt


def concat(parts: List[str], out_path: str,
           progress_cb: Optional[Callable] = None) -> str:
    """把多支影片接成一支。回輸出路徑。"""
    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    parts = [p for p in parts if p and os.path.isfile(p)]
    if not parts:
        raise RuntimeError("沒有可合併的片段。")
    if len(parts) == 1:
        shutil.copyfile(parts[0], out_path)
        return out_path

    whs = [_probe_wh(p) for p in parts]
    uniform = all(wh and wh == whs[0] for wh in whs)

    if uniform:
        lst = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                          delete=False, encoding="utf-8")
        try:
            for p in parts:
                # 正斜線 + 單引號跳脫：Windows 反斜線與空白會讓 concat demuxer 解析失敗
                ap = os.path.abspath(p).replace("\\", "/").replace("'", "'\\''")
                lst.write(f"file '{ap}'\n")
            lst.close()
            say(90, f"接合 {len(parts)} 段（串流複製，無損）…")
            r = _media.ffmpeg(["-f", "concat", "-safe", "0", "-i", lst.name,
                               "-c", "copy", out_path])
            if r.returncode == 0:
                return out_path
            say(90, "串流複製接不起來，改用重編…")
        finally:
            try:
                os.unlink(lst.name)
            except OSError:
                pass

    w, h = whs[0] if whs[0] else (1920, 1080)
    inputs, filt, labels = [], [], []
    for i, p in enumerate(parts):
        inputs += ["-i", p]
        filt.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}];"
            f"[{i}:a]aresample=async=1[a{i}];")
        labels.append(f"[v{i}][a{i}]")
    filt.append("".join(labels) + f"concat=n={len(parts)}:v=1:a=1[outv][outa]")

    # filter 字串會很長，一定要走檔案；讀檔的選項名在 ffmpeg 8.0 換過，
    # 交給 media.filter_script_args() 判斷，不要自己寫死。
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                      delete=False, encoding="utf-8")
    try:
        tmp.write("".join(filt))
        tmp.close()
        say(90, f"接合 {len(parts)} 段（重編、統一 {w}x{h}）…")
        r = _media.ffmpeg(inputs + _media.filter_script_args(tmp.name) +
                          ["-map", "[outv]", "-map", "[outa]",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                           "-r", "30", "-c:a", "aac", "-b:a", "192k", out_path])
        if r.returncode != 0:
            tail = "\n".join((r.stderr or "").strip().splitlines()[-12:])
            raise RuntimeError(
                "接合失敗，ffmpeg 回報：\n" + tail +
                "\n\n各段的淨毛片都還在，可以先個別使用。")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return out_path


def process_folder(folder: str, output_dir: Optional[str] = None,
                   output_name: Optional[str] = None,
                   profile: Optional[str] = None,
                   force: bool = False,
                   progress_cb: Optional[Callable] = None) -> dict:
    """一個資料夾 = 一支長片：各段各自順剪，再接成一支完整淨毛片。

    回 `{parts, cleans, output, srt}`。
    """
    from .pipeline import analyze

    def say(p, m):
        if progress_cb:
            progress_cb(p, m)

    if not os.path.isdir(folder):
        raise NotADirectoryError(
            f"找不到資料夾：{folder}\n"
            "長片模式吃的是「一個資料夾」，裡面放同一支片的各個段落。")

    parts = find_parts(folder)
    if not parts:
        raise RuntimeError(
            f"這個資料夾裡沒有可以處理的影片：{folder}\n"
            f"支援的格式：{'、'.join(VIDEO_EXTS)}")

    out_root = output_dir or folder
    os.makedirs(out_root, exist_ok=True)

    say(0, f"找到 {len(parts)} 段，依序順剪後接成一支：")
    for i, p in enumerate(parts, 1):
        say(0, f"  第 {i} 段：{os.path.basename(p)}")

    cleans, srts = [], []
    for i, p in enumerate(parts, 1):
        prefix = f"[{i}/{len(parts)}] {os.path.basename(p)}"
        base = os.path.splitext(os.path.basename(p))[0]
        expect = os.path.join(out_root, f"{base}_淨毛片.mp4")
        if not force and os.path.isfile(expect):
            # 長片很久，中途失敗重跑時不該把已經剪好的段落再剪一次
            say(0, f"{prefix}：已經剪過，沿用（要重剪加 --force）")
            cleans.append(expect)
            srts.append(os.path.join(out_root, f"{base}_字幕.srt"))
            continue
        say(0, f"{prefix}：順剪中…")
        # 每段的進度壓進整體進度，不然使用者會看到進度條反覆從 0 跑到 100
        lo = int(85 * (i - 1) / len(parts))
        hi = int(85 * i / len(parts))
        res = analyze(p, do_cut=True, output_dir=out_root, profile=profile,
                      progress_cb=lambda pc, m: say(lo + (hi - lo) * pc // 100,
                                                    f"{prefix}：{m}"))
        cleans.append(res["output"])
        srts.append(res.get("srt"))
        s = res["plan_data"]["summary"]
        say(hi, f"{prefix}：完成（{s['原長秒']}s → {s['剪後秒']}s）")

    name = output_name or os.path.basename(os.path.normpath(folder))
    final = os.path.join(out_root, f"{name}_完整淨毛片.mp4")
    concat(cleans, final, progress_cb=progress_cb)

    final_srt = None
    try:
        say(96, "合併字幕時間軸…")
        final_srt = merge_srts(list(zip(cleans, srts)),
                               os.path.join(out_root, f"{name}_完整字幕.srt"))
    except Exception as e:
        # 字幕合不起來不該讓整件事失敗——影片已經接好了，各段也還有自己的 SRT
        say(96, f"合併字幕失敗（各段的字幕都還在）：{e}")

    say(100, f"完成：{os.path.basename(final)}")
    return {"parts": parts, "cleans": cleans, "output": final, "srt": final_srt}
