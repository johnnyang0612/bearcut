---
name: bearcut-brain
description: 幫使用者補上 BearCut 的判斷腦，讓它從「只會剪靜音」升級成會讀語意的順剪。當 doctor 回報「判斷腦：無」、使用者問「為什麼只剪掉靜音」「怎麼變聰明」，或你是在 Claude Cowork／Codex 這類 agent 環境裡操作 BearCut 時使用。
---

# 補上 BearCut 的判斷腦

## 先搞懂一件事（**這是最常見的誤解**）

你正在操作 BearCut。你會思考。但**你不是 BearCut 的判斷腦。**

| 角色 | 是誰 | 怎麼運作 |
|---|---|---|
| **操作者** | 你（Claude Code、桌面版 Code 分頁、Codex、Cursor…） | 打指令、讀 JSON、把結果講給使用者聽 |
| **判斷腦** | 一顆 PATH 上的 CLI，或一組 API 金鑰 | BearCut 自己開子行程／發 HTTP 呼叫它，一支片會叫上幾十次 |

BearCut 在剪片途中要問幾十輪「這句是不是重講」「這個字是不是聽錯」，
它必須**自己能呼叫**判斷腦，不能停下來等你回話。所以即使操作者是你，
機器上還是得有一顆判斷腦，這不是重複投資。

沒有判斷腦不會失敗，只會降級成**剪靜音與卡頓**——使用者通常會覺得「跟其他自動剪輯沒兩樣」，
所以值得花三分鐘補上。

## 判斷現況

```bash
python cli.py doctor --json
```

看 `llm.ok`：

- `true` → 已經有了，看 `llm.selected` 是誰，直接去剪片
- `false` → 往下走。`checks.llm.fix` 是完整說明，`checks.llm.fix_command` 是可直接執行的指令

## 引導使用者安裝

**先用白話講清楚代價與好處**，不要直接丟指令給他：

> 現在 BearCut 只會剪靜音跟卡頓。要讓它讀懂你在講什麼、剪掉重講跟講壞的段落，
> 需要在這台電腦上裝一個小程式（Claude Code）。
> **如果你已經在付 Claude 的月費，用同一個帳號登入就好，不用另外付錢、也不用申請金鑰。**
> 大概三分鐘。

同意之後，依平台執行：

```powershell
# Windows —— PowerShell
irm https://claude.ai/install.ps1 | iex
```

```bash
# macOS / Linux / WSL
curl -fsSL https://claude.ai/install.sh | bash
```

> 如果使用者說「我已經有 Claude 桌面版了」——**還是要裝**。桌面版內建 Claude Code，
> 但不會在 PATH 上放 `claude` 指令（官方原文：「To use `claude` from the terminal,
> install the CLI separately」），BearCut 呼叫不到。裝完兩者共用同一個登入。

裝完驗證與登入：

```bash
claude --version    # 印出版本號就是裝好了
claude              # 開一次，跟著瀏覽器提示登入，然後離開
```

⚠ **登入這一步必須由使用者本人做**（會開瀏覽器要他授權）。你不要嘗試代替他點，
就停在這裡告訴他「瀏覽器會跳出來，登入完跟我說一聲」。

最後回頭確認：

```bash
python cli.py doctor --json     # llm.ok 應該變成 true
```

## 如果他沒有 Claude 付費方案

Claude Code 需要 Pro / Max / Team / Enterprise / Console 帳號，**免費方案不能用**。
他沒有的話，兩條退路：

1. **改用 API 金鑰**——設環境變數 `ANTHROPIC_API_KEY`（或 `OPENAI_API_KEY`、`GEMINI_API_KEY`），
   按用量計費，剪一支片通常只有幾塊錢
2. **完全本機、免費**——他自己有跑 Ollama / LM Studio 的話，設 `BEARCUT_LLM=ollama`
   走 OpenAI 相容端點；品質會比雲端模型差一些，但不用付錢也不外傳資料

兩條都不要就照現況用，剪靜音也是有價值的，**不要讓他覺得非裝不可**。

## 不要做

- 不要用 `sudo` 裝，官方明講會造成權限問題
- 不要幫他改系統 PATH——安裝程式自己會處理
- 不要因為自己是 AI agent 就試圖「幫 BearCut 做判斷」：判斷結果要能被程式取用，
  用聊天回覆餵不進去（`bearcut/llm/base.py` 的 `Provider` 介面是唯一入口）
