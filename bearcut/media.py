# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""ffmpeg / ffprobe 的呼叫層。

**所有** ffmpeg 呼叫都要走這裡，不要在別的模組直接 `subprocess.run(["ffmpeg", ...])`。
兩個理由：

1. **一律用 vendor/ 自帶的那份。** 使用者系統上的 ffmpeg 版本與編譯選項不可控，
   自己帶一份才能保證兩台機器輸出一致（這是 P0 就定下的原則）。
2. **編碼統一處理。** Windows 中文系統 locale 是 cp950，但 ffmpeg 輸出含 UTF-8
   中文檔名。用 `text=True` 走 locale 解碼會 UnicodeDecodeError，連帶整個結果吃不到。
"""

import os
import subprocess
import tempfile
from typing import List, Optional

from .env import ffmpeg as _ff


class MediaError(RuntimeError):
    """ffmpeg / ffprobe 執行失敗。訊息要讓使用者知道下一步怎麼辦。"""


def _binary(name: str) -> str:
    p = _ff.find(name)
    if not p:
        raise MediaError(
            f"找不到 {name}。請執行 python bootstrap.py 讓它自動下載，"
            "或自行安裝並確認在 PATH 中。")
    return str(p)


def run(cmd: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """執行並強制 UTF-8 解碼（見模組說明第 2 點）。

    只給輸出量必然很小的呼叫用（ffprobe、能力探測）。跑 ffmpeg 請走 `ffmpeg()`
    ——那裡改走暫存檔，理由見 `run_to_files()`。
    """
    return subprocess.run(
        cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)


# 讀回輸出時的上限。超過就只留頭尾——中間那段對診斷沒有價值，
# 而「無條件全部讀進記憶體」正是這裡要避免的事。
_CAP = 4 * 1024 * 1024


def _read_capped(path: str) -> str:
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size <= _CAP:
                data = f.read()
            else:
                head = f.read(_CAP // 2)
                f.seek(size - _CAP // 2)
                data = head + "\n…（輸出過長，中間省略）…\n".encode("utf-8") + f.read()
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def run_to_files(cmd: List[str],
                 timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """執行，但把 stdout/stderr 導到暫存檔而不是 pipe，讀回來時再自己設上限。

    **為什麼不用 `capture_output`。** subprocess 的 reader thread 是用一次
    `read()` 把整串輸出讀成單一字串，沒有任何上限。ffmpeg 若持續吐訊息（進度行、
    逐格重複的 warning），長片編碼下這個字串會一直長到 MemoryError；而 reader
    thread 死掉之後 `subprocess.run` 會**永遠**等它 join，外觀就是整支程式無聲卡死。

    實戰來源：AutoCut 2026-08-09 切 88 分鐘直播，第 3 支起穩定卡死，最久卡了
    48 分鐘才被發現。`-nostats` 擋掉主要來源（見 `ffmpeg()`），但那只是把量壓小，
    沒有把「無上限」這件事修掉——會逐格重複的 warning 一樣能撐爆。導到檔案之後
    輸出量再大也只是佔硬碟，記憶體由 `_CAP` 決定。
    """
    o = tempfile.NamedTemporaryFile(suffix=".ffout", delete=False)
    e = tempfile.NamedTemporaryFile(suffix=".fferr", delete=False)
    o.close()
    e.close()
    try:
        with open(o.name, "wb") as fo, open(e.name, "wb") as fe:
            p = subprocess.run(cmd, stdout=fo, stderr=fe, timeout=timeout)
        return subprocess.CompletedProcess(
            cmd, p.returncode, _read_capped(o.name), _read_capped(e.name))
    finally:
        for n in (o.name, e.name):
            try:
                os.unlink(n)
            except OSError:
                pass


def ffmpeg(args: List[str], timeout: Optional[int] = None,
           loglevel: str = "error") -> subprocess.CompletedProcess:
    """跑 ffmpeg。**一律帶 `-y`**。

    為什麼放在這裡而不是各自寫：沒帶 `-y` 時 ffmpeg 遇到已存在的輸出檔會等
    使用者確認，而我們沒有 tty，結果是**安靜地失敗**。BearCut 的輸出路徑是
    從輸入檔名推出來的固定值（`_淨毛片.mp4`、`_封面.jpg`…），所以「重跑一次」
    必然撞到已存在的檔——而重跑正是使用者最常做的事。

    實際踩過：有些呼叫端記得帶、有些忘了，忘了的那幾支要等到有人重跑第二次
    才會發現。集中在這裡就不會再漏。

    `loglevel` 預設 `error`：出錯要看得到，但不要一路吐 info。**只有需要讀
    ffmpeg 正常輸出的呼叫端才改它**——目前只有 `detect/silence.py`，
    silencedetect 的結果是印在 info 層的 stderr，壓成 error 會整層靜音偵測失效
    （而且是安靜地失效：沒有錯誤訊息，只是一刀都沒抓到）。
    """
    return run_to_files(
        [_binary("ffmpeg"), "-hide_banner", "-v", loglevel, "-nostats", "-y"] + args,
        timeout)


def ffprobe(args: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return run([_binary("ffprobe"), "-v", "error"] + args, timeout)


_filter_opt = None


def filter_script_args(path: str) -> List[str]:
    """回傳「從檔案讀 filter_complex」的正確參數，依 ffmpeg 版本自動選。

    為什麼要偵測：保留段一多，filter 字串輕易上萬字元，會超過 Windows 命令列上限，
    所以一定要走檔案。但這個選項的寫法在 ffmpeg 8.0 換過：

    - 舊版：`-filter_complex_script <file>`
    - 新版：`-/filter_complex <file>`（通用的「選項值從檔案讀」語法）

    我們自帶的 ffmpeg 是新版，但使用者系統上可能是舊版，兩邊都要能跑。
    偵測一次就快取——探測是讓 ffmpeg 立刻失敗，很便宜。
    """
    global _filter_opt
    if _filter_opt is None:
        probe = run([_binary("ffmpeg"), "-hide_banner", "-filter_complex_script"])
        _filter_opt = ("-filter_complex_script"
                       if "Unrecognized option" not in (probe.stderr or "")
                       else "-/filter_complex")
    return [_filter_opt, path]


def get_duration(path: str) -> float:
    """影片總長度（秒）。"""
    out = ffprobe(["-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", path])
    if out.returncode != 0:
        raise MediaError(f"讀取影片長度失敗：{(out.stderr or '').strip()[:300]}")
    try:
        return float((out.stdout or "").strip())
    except ValueError:
        raise MediaError(
            f"影片長度無法解析：{(out.stdout or '').strip()[:100]}\n"
            "這個檔案可能損毀，或不是 ffmpeg 認得的影片格式。")
