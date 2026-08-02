# 規則包授權 / Rulepack License

**繁體中文** ｜ [English](#english)

## 為什麼規則包跟程式碼分開授權

BearCut 的**程式碼**採用 Apache License 2.0 —— 可自由使用、修改、再散布，包含商業用途。

`rulepack/` 底下的東西不是程式碼，是**內容**：偵測門檻、判斷用的 prompt、
繁體錯詞對照表、字幕與字卡樣式。這些是持續調校累積出來的成果，每個月都還在變。

程式碼開源是為了讓你信任它、修改它、驗證它剪得對不對。
**規則包公開可讀也是刻意的** —— 你要看得懂它憑什麼這樣剪，才有辦法微調成你要的手感。
但「可讀」不等於「可以抽出來當自己的產品賣」。這份文件把界線畫清楚。

## 適用範圍

本文件涵蓋 `rulepack/` 目錄下的所有檔案，包含但不限於：

- `thresholds.json` —— 各層偵測門檻
- `prompts/` —— 判斷腦使用的提示詞
- `replacements.json` —— 繁體錯詞修正對照
- `rulepack.json` —— 規則包描述檔

著作權為 **川輝科技有限公司（Brightstream Technology Co., Ltd.）**所有。
**不隨程式碼的 Apache-2.0 授權釋出。**

## ✅ 你可以

- **使用**這些規則剪你自己的片，個人或商業用途都可以，不限數量、不限時數
- **閱讀、研究**內容，理解它為什麼這樣判斷
- **修改**成適合你素材的版本，自己用或在你的團隊內部用
- 在文章、教學、評論中**引用**片段來說明運作方式
- 隨**未修改**的 BearCut 一起再散布，保留本檔案

## ❌ 你不可以

- 把規則包**單獨抽出來**重新散布、上架或販售
- 將其內容（含修改後的版本）**包裝成競品**的規則庫、預設集或訂閱服務
- 移除或變更本授權聲明與著作權標示

## 進階規則包

BearCut Pro 的訂閱規則包適用**個別授權條款**，隨該規則包一併提供，
其條款優先於本文件。簡單講：Pro 包是給訂閱者本人使用的，不得轉散布。

## 無擔保

規則包按「現狀」提供，不附任何明示或默示的擔保。剪輯結果請自行複核 ——
這也是 BearCut 一律輸出「剪了哪些、為什麼」對照表的原因。

## 有疑問

不確定你的用法算不算合理，到官網聯絡我們就好，我們通常會說可以：
**https://Brightstream.com.tw**

---

<a name="english"></a>

# Rulepack License (English)

## Why the rulepack is licensed separately

The **source code** of BearCut is licensed under the Apache License 2.0 — free to
use, modify, and redistribute, including commercially.

The contents of `rulepack/` are not code; they are **content**: detection
thresholds, prompts, a Traditional Chinese correction table, and subtitle and
title-card styling. These are the result of continuous tuning and still change
every month.

The code is open so you can trust it, modify it, and verify that it cuts
correctly. **The rulepack is readable on purpose, too** — you need to see why it
cuts the way it does in order to tune it to your own taste. But "readable" does
not mean "may be extracted and sold as your own product." This file draws that
line.

## Scope

This document covers all files under `rulepack/`, including but not limited to
`thresholds.json`, `prompts/`, `replacements.json`, and `rulepack.json`.

Copyright **Brightstream Technology Co., Ltd. (川輝科技有限公司)**.
**Not licensed under Apache-2.0.**

## ✅ You may

- **Use** these rules to edit your own videos, personally or commercially, with
  no limit on volume or hours
- **Read and study** the contents to understand the reasoning
- **Modify** them to suit your footage, for your own or your team's internal use
- **Quote** excerpts in articles, tutorials, and reviews to explain how it works
- Redistribute them alongside an **unmodified** copy of BearCut, keeping this file

## ❌ You may not

- **Extract** the rulepack and redistribute, publish, or sell it on its own
- Repackage its contents (including modified versions) as a **competing** rule
  library, preset collection, or subscription service
- Remove or alter this license notice or the copyright attribution

## Pro rulepacks

Subscription rulepacks for BearCut Pro are covered by **separate terms**
delivered with those packs, which take precedence over this document. In short:
a Pro pack is licensed to the subscriber and may not be redistributed.

## No warranty

Rulepacks are provided "as is", without warranty of any kind. Always review the
output — which is why BearCut always emits a "what was cut and why" report.

## Questions

If you are unsure whether your use is acceptable, just ask via our website —
the answer is usually yes: **https://Brightstream.com.tw**
