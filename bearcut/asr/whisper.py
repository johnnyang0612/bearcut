# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""whisper 辨識（順稿）—— 提供**文字內容**，不提供切點時間。

與 Paraformer 的分工：
- **whisper**：繁體中文乾淨順稿，用來產字幕、給判斷層讀語意
- **Paraformer**：字級精準時間戳，**所有切點的時間來源**

whisper 會「順稿」——把重講、結巴、贅字自動吞掉。那對字幕是優點，
對我們的偵測是致命的，所以時間一律不用它的。

## 毛片預剪的兩個關鍵設定
- `vad_filter=False`：毛片要連停頓與空白都涵蓋。靜音另外用 ffmpeg silencedetect 抓，
  若讓 whisper 的 VAD 先把靜音吃掉，我們就抓不到要剪的空白了。
- `word_timestamps=True`：給下游做卡頓偵測（字間異常空檔）。
"""

import os
from typing import Callable, List, Optional

from ..env.platform import VENDOR

# 模型下載落點放專案內，符合「所有東西都在這個資料夾裡」的原則，
# 也讓使用者刪掉資料夾就真的清乾淨，不會在家目錄留下好幾 GB 的快取。
MODELS_DIR = VENDOR / "models"

_CACHE = {}


def _build(model_size: str, device: str, compute: str):
    # 必須在 import faster_whisper（連帶載入 CTranslate2）之前掛上 nvidia 的 DLL 目錄，
    # 否則 pip 裝的 cuBLAS/cuDNN 在 Windows 上找不到 —— 見 env/cuda.py 的說明。
    if device == "cuda":
        from ..env import cuda as _cuda
        _cuda.enable_dll_search()
    from faster_whisper import WhisperModel
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return WhisperModel(model_size, device=device, compute_type=compute,
                        download_root=str(MODELS_DIR))


def _load_model(model_size: str = "large-v3", device: Optional[str] = None):
    """載入模型。回 `(model, device, compute)`。

    ⚠️ **建構成功不代表能用。** CTranslate2 只要偵測到顯示卡就接受 `device="cuda"`，
    但實際推論時才會去載 cuBLAS——若那些 DLL 不在（很常見，因為 PyPI 上的 torch
    在 Windows 預設是 CPU 版，不會帶 CUDA 函式庫），會在第一次 encode 才炸，
    而那時模型已經「載入成功」了，建構期的 try/except 完全攔不到。

    所以這裡**只負責建構**，能不能真的跑由 `transcribe_words` 在推論失敗時退回。
    對「不必自己裝 CUDA 也要能用」這個目標來說，CPU 是可靠預設，GPU 是加分項。
    """
    device = device or os.environ.get("BEARCUT_WHISPER_DEVICE") or "cuda"
    compute = "float16" if device == "cuda" else "int8"
    key = (model_size, device)
    if key in _CACHE:
        return _CACHE[key]

    try:
        model = _build(model_size, device, compute)
    except Exception as e:
        if device == "cuda":
            return _load_model(model_size, device="cpu")
        raise RuntimeError(
            f"無法載入 whisper 模型 {model_size}：{e}\n"
            "常見原因：第一次使用時模型還在下載（約 1.5GB，請確認網路連線）、"
            "或磁碟空間不足。") from e

    _CACHE[key] = (model, device, compute)
    return _CACHE[key]


def transcribe_words(
    input_path: str,
    language: str = "zh",
    model_size: str = "large-v3",
    hotwords: Optional[list] = None,
    initial_prompt: Optional[str] = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> List[dict]:
    """辨識語音，回傳分段列表。

    每段：`{"start": float, "end": float, "text": str,
            "words": [{"start", "end", "word"}, ...]}`，時間單位為秒。
    """
    def report(p, msg):
        if progress_cb:
            progress_cb(p, msg)

    report(5, f"載入 {model_size} 模型中…")
    model, device, compute = _load_model(model_size)
    report(15, f"模型就緒（{device}/{compute}），開始辨識毛片…")

    def run_with_fallback(kw):
        """跑辨識；GPU 在推論階段失敗就退回 CPU 重跑。

        見 _load_model 的說明——CUDA 的問題要到第一次 encode 才會浮現。
        """
        nonlocal model, device, compute
        try:
            return _run(model, input_path, kw, report)
        except Exception as e:
            if device != "cuda":
                raise
            report(15, f"⚠ GPU 辨識失敗（{type(e).__name__}），改用 CPU 重跑…\n"
                       "     （若想用 GPU，需安裝 CUDA 版 PyTorch 與 cuBLAS）")
            _CACHE.pop((model_size, "cuda"), None)
            model, device, compute = _load_model(model_size, device="cpu")
            return _run(model, input_path, kw, report)

    kwargs = dict(
        language=language,
        beam_size=5,
        vad_filter=False,          # 毛片預剪：不丟靜音（見模組說明）
        word_timestamps=True,
    )
    if hotwords:
        kwargs["hotwords"] = " ".join(hotwords)
    if initial_prompt:
        # 引導 whisper 用字（例：繁體中文）。whisper 的 zh 繁簡看運氣——實測同一支片
        # 第一輪吐繁體、第三輪吐簡體，所以下游仍須 zhconv 保底，不能只靠這個。
        kwargs["initial_prompt"] = initial_prompt

    # 刻意不用 BatchedInferencePipeline：實測會整段漏字幕。
    segments = run_with_fallback(kwargs)

    # 防呆：whisper large-v3 偶爾在重複或卡頓的音訊上「迴圈幻覺」——把同一句連吐十幾遍，
    # 把那段真實內容整個蓋掉（實測某片 50~87 秒被吐成 16 段一模一樣的句子）。
    # 偵測到就關掉 condition_on_previous_text 重辨識，該選項會切斷「依前文續寫」的迴圈。
    # 只在出問題時才啟用，避免它的副作用影響正常影片。
    if _has_repeat_loop(segments):
        report(85, "偵測到 whisper 重複迴圈幻覺，改參數重新辨識…")
        kwargs2 = {**kwargs, "condition_on_previous_text": False, "temperature": 0.0}
        segments2 = run_with_fallback(kwargs2)
        if segments2 and not _has_repeat_loop(segments2):
            segments = segments2
        else:
            report(85, "重辨識仍有迴圈，收斂連續重複段以免誤砍整片")
            segments = _collapse_repeat_loops(segments2 or segments)

    report(90, f"辨識完成，共 {len(segments)} 段")
    return segments


def _run(model, input_path: str, kwargs: dict, report) -> List[dict]:
    segments_gen, _info = model.transcribe(input_path, **kwargs)
    out = []
    for i, seg in enumerate(segments_gen):
        words = []
        for w in (seg.words or []):
            if w.start is None or w.end is None:
                continue
            words.append({"start": float(w.start), "end": float(w.end), "word": w.word})
        out.append({"start": float(seg.start), "end": float(seg.end),
                    "text": seg.text.strip(), "words": words})
        if (i + 1) % 5 == 0:
            report(min(20 + i * 2, 85), f"已辨識 {i + 1} 段…")
    return out


def _has_repeat_loop(segments: List[dict], run: int = 4) -> bool:
    """連續 >= run 段文字一模一樣 → 判定為迴圈幻覺。"""
    streak = 1
    for i in range(1, len(segments)):
        t = segments[i]["text"].strip()
        if t and t == segments[i - 1]["text"].strip():
            streak += 1
            if streak >= run:
                return True
        else:
            streak = 1
    return False


def _collapse_repeat_loops(segments: List[dict], run: int = 3) -> List[dict]:
    """把連續一樣的段收斂成一段（保留整體時間範圍）。

    至少不讓重複偵測把整片誤砍掉——幻覺造成的假重複，寧可少剪也不要多剪。
    """
    if not segments:
        return segments
    out = [dict(segments[0])]
    for seg in segments[1:]:
        if seg["text"].strip() and seg["text"].strip() == out[-1]["text"].strip():
            out[-1]["end"] = seg["end"]
            out[-1]["words"] = out[-1].get("words", []) + seg.get("words", [])
        else:
            out.append(dict(seg))
    return out
