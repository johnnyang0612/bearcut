---
name: bearcut-setup
description: 把 BearCut 的執行環境準備好（虛擬環境、Python 套件、FFmpeg、GPU 加速）。當使用者第一次要用 BearCut、或 doctor 回報環境不完整時使用。
---

# 準備 BearCut 環境

## 步驟

**1. 先確認現況** —— 不要跳過，環境沒好會在辨識到一半才失敗，浪費好幾分鐘。

```bash
python cli.py doctor --json
```

`ok` 為 `true` 就不用做任何事，直接告訴使用者可以開始了。

**2. 缺什麼就補**

```bash
python bootstrap.py
```

這支會自己處理：建虛擬環境 → 裝 Python 套件 → 偵測到 NVIDIA 顯卡時裝 GPU 版
PyTorch → 下載 FFmpeg 到 `vendor/`。

⏱ **這會跑 10-30 分鐘**（要下載數 GB）。這是正常的。
**不要設短逾時、不要中途打斷。** 用背景執行並在完成時回報。

**3. 再驗一次**

```bash
python cli.py doctor
```

把結果用白話講給使用者聽，不要貼原始 JSON。

## 常見狀況

| doctor 顯示 | 意思 | 處理 |
|---|---|---|
| 判斷腦：無 | 沒有 claude/codex CLI 也沒有 API 金鑰 | **不擋**。走 **bearcut-brain** skill 引導他補上——即使正在操作 BearCut 的是你（AI agent）也一樣需要，你是操作者不是判斷腦 |
| 偵測到顯卡但缺 CUDA 函式庫 | torch 裝到 CPU 版 | 跑 `python bootstrap.py` 會自動修 |
| FFmpeg 找不到 | 尚未下載 | `python cli.py setup` 單獨補 |

## 不要做

- 不要幫使用者去改系統 PATH 或安裝系統層級的 CUDA —— BearCut 所有東西都裝在專案資料夾內，這是刻意的設計。
- 不要因為「判斷腦：無」就認為安裝失敗，那是預期中的降級路徑。
