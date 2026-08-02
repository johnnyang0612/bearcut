# CLAUDE.md

Claude Code 在這個專案工作時的指引。**動手改程式之前請先讀完。**

## 這是什麼

BearCut：自動順剪工具。吃一支毛片，剪掉重講、講壞、口吃、卡頓、長靜音，
輸出淨毛片、字幕、以及一份「剪了哪些、為什麼」的複核對照表。

面向不會寫程式的使用者。**所有 UI 文案繁體中文。**

## 指令

```bash
python bootstrap.py               # 首次安裝（venv + 套件 + FFmpeg + GPU）
python cli.py doctor              # 環境自檢（缺什麼、怎麼補）
python cli.py cut <影片>           # 順剪：辨識 → 偵測 → 剪
python cli.py cut <影片> --mode fast|balanced|precise
python cli.py cut <影片> --plan-only    # 只產清單給人複核
python cli.py cut --apply <清單.json>   # 吃複核過的清單只剪片
```

全部指令支援 `--json`（給程式與 AI Agent 解析）。

沒有測試套件。**驗證方式是實際跑黃金樣本並比對輸出**：拿同一支毛片，比對
`_待剪清單.json` 的剪掉比例、分層刀數（靜音／卡頓／重講／重複）、切點時間偏移、
以及保留區間的 IoU。ASR 每輪分段本來就略有差異，所以標準是「差異在容許範圍」，
不是逐位元相同。

## 架構

想理解整個系統，**從 `bearcut/pipeline.py` 讀起**——它的註解說明每一層的取捨。

```
bearcut/
  env/        平台偵測、FFmpeg、GPU、自檢   ← 只用標準函式庫（bootstrap 會在裝套件前 import）
  asr/        whisper（文字）+ Paraformer（時間）
  detect/     silence・gaps・restarts・redundant・fragments・safety
  llm/        判斷腦：本機 CLI（claude/codex）+ API（Anthropic/OpenAI/Gemini/相容）
  correct.py  校字（必須在判斷之前）
  plan.py     合併刀 → 保留區間 → 對照表
  cut.py      ffmpeg 剪接
  subtitle.py V1 + SRT（時間軸對齊剪完之後）
  subs.py     繁體斷詞與斷行
rulepack/     門檻與 prompt（不寫死在程式碼裡，可單獨更新）
```

## 五條不可違反的設計原則

**1. 語意判斷與時間戳職責分離。**
LLM 只決定「剪什麼內容」，**絕不信任它回傳的秒數**。所有切點時間一律由
Paraformer 字級時間戳反查（`refine_cut_to_words`、`align_cut_to_text`、
`snap_cuts_off_chars`）。這是全系統的鐵則，違反會造成誤剪。

**2. 先校字、再判斷。**
whisper 會把「一人公司」聽成「藝人公司」。順序反了，判斷腦會把講得很好的段落
當廢段刪掉。

**3. 每一層都有獨立的暴走安全閥——但只套在會產生幻覺的層。**
適用：重講、破碎片段、判斷腦。**不適用：靜音、卡頓**（確定性訊號處理不會暴走，
套上去會把正常的 38 段靜音整層作廢）。

**4. 確定性偵測保底 + 判斷腦補抓。**
每一種 LLM 提議都有程式層護欄擋在後面，對不上就整組不剪。

**5. 沒有判斷腦也要能跑。**
降級成只剪靜音與卡頓，不是失敗。

## ⚠️ 改動前必讀

這個專案踩過的坑裡，有幾個是**「看起來像 bug 但不能改」**的。最經典的一個：

> `detect/gaps.py` 的門檻是 `keep_sec + edge_pad_sec*2`，而設定裡的 `min_gap_sec`
> 從未被使用。這看起來像 bug，但那個行為正是既有成品的品質來源——
> 移植時「修正」它會讓卡頓偵測全部失效。

**看到不合理的地方，先確認那個不合理有沒有被輸出依賴。**

其他高頻坑：
- `modelscope` 必須鎖 1.20.0（新版會讓字級時間戳整層失效）
- `opencv-python` 必須鎖 4.x（5.0 拿掉內建 Haar）
- Windows 主控台 cp950 遇非 ASCII 會炸掉進度回報 → 每個進入點都要 `console_utf8()`
- `.gitignore` 的行尾註解無效（git 只認整行開頭的 `#`）
- jieba 隨 wheel 只有簡體詞典，繁體要繞路（見 `subs.py`）

## 慣例

- commit message 中文，說明**為什麼**這樣改，不只是改了什麼
- 門檻與 prompt 一律進 `rulepack/`，不要寫死在程式碼裡
- 錯誤訊息要寫成「說明原因 + 下一步怎麼辦」，使用者看得懂
- 品牌素材在 `assets/brand/`，**不在 Apache 授權範圍內**（見 TRADEMARK.md）
