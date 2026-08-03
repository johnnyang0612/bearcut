# AGENTS.md — 給 AI Agent 的操作說明

> 人類請看 [README.md](README.md)。

你被交付了一個 BearCut 資料夾。它是一個**自動順剪工具**：吃一支毛片，
剪掉重講、講壞、口吃、卡頓、長靜音，輸出淨毛片與字幕。

## 先確認你是哪一種

**你能執行終端機指令嗎？**（Claude Code、Codex、Cursor 這類可以）

- **可以** → 往下讀，照著做。
- **不行**（你是 ChatGPT／Claude 網頁版這種對話型 AI，使用者把資料夾或
  壓縮檔丟給你）→ **你不能代替他執行**，請直接告訴他這幾句話：

  > 這個資料夾要在你自己的電腦上執行，我沒辦法代替你跑。
  >
  > 1. Windows 的話雙擊 `START_HERE.bat`，macOS 的話是 `START_HERE.command`
  >    （macOS 第一次要按右鍵→開啟，不能雙擊）。
  > 2. 第一次會下載約 2–3 GB 的模型，跑完瀏覽器會自動打開。
  > 3. 把影片拖進去就會開始剪。
  >
  > 如果想讓 AI 幫你操作，要用「能執行指令」的工具（例如 Claude Code），
  > 把這個資料夾交給它，它讀 `AGENTS.md` 就知道怎麼做。

  然後就停在這裡，不要試著解釋程式碼或幫他改設定——他要的是把片剪出來，
  不是理解這個專案。

## 你要做的第一件事

```bash
python cli.py doctor --json
```

讀回傳 JSON 的 `ok` 欄位：

| `ok` | 意思 | 你該做什麼 |
|---|---|---|
| `true` | 環境就緒 | 直接往下做事 |
| `false` | 缺東西 | 看 `blocking` 陣列，然後執行 `python bootstrap.py` |

`bootstrap.py` 會建虛擬環境、裝套件、下載 FFmpeg、必要時自動裝 GPU 版 PyTorch。
**它可能跑 10-30 分鐘**（要下載數 GB 的模型與函式庫），這是正常的，不要中途打斷。

裝完再跑一次 `doctor --json` 確認。

## 你是操作者，不是判斷腦

**這是最容易搞錯的一點。** 你會思考，但 BearCut 的語意判斷不能由你提供：

| 角色 | 是誰 | 怎麼運作 |
|---|---|---|
| **操作者** | 你（Claude Cowork、Codex、Claude Code、Cursor…） | 打指令、讀 JSON、回報給使用者 |
| **判斷腦** | PATH 上的一顆 CLI，或一組 API 金鑰 | BearCut 自己開子行程／發 HTTP，一支片問幾十輪 |

剪片途中要問幾十次「這句是不是重講」「這個字有沒有聽錯」，BearCut 必須**自己呼叫得到**，
不能停下來等你回話——所以判斷腦得是程式介面（`bearcut/llm/base.py` 的 `Provider`），
你的聊天回覆餵不進去。

`doctor --json` 的 `llm.ok` 為 `false` 時：不是錯誤，是降級成只剪靜音與卡頓。
要補上判斷腦，`checks.llm.fix_command` 就是可直接執行的指令（依平台給好），
完整引導見 **bearcut-brain** skill。

> 若使用者已經在付 Claude 的月費（例如他就是用 Cowork 在跟你講話），
> 裝 `claude` CLI 後用同一個帳號登入即可，**不必另外申請或購買 API 金鑰**。
> 裝好之後他照樣用原本的方式操作，那顆 CLI 只是躺在硬碟上被 BearCut 背景呼叫。

## 剪一支影片

```bash
python cli.py cut <影片路徑> --json
```

回傳：
```json
{"ok": true,
 "output": "…_淨毛片.mp4",
 "plan": "…_待剪清單.json",
 "review": "…_剪輯判斷對照.txt",
 "v1": "…_字幕校對_V1.txt",
 "srt": "…_字幕.srt",
 "summary": {"原長秒": 97.8, "剪後秒": 85.5, "剪掉比例": "12.7%", …}}
```

### 模式（依使用者要求選）

```bash
--mode fast       # 快 2-3 倍、token 約 1/3，準確度略降
--mode balanced   # 預設
--mode precise    # 不計成本求準，慢 2-3 倍、token 約 3 倍
```

使用者說「快一點」「先看個大概」「素材很多」→ `fast`。
說「要準」「交件用」「重要」→ `precise`。

### 其他常用

```bash
python cli.py cut <影片> --plan-only    # 只產清單給人複核，先不剪
python cli.py cut --apply <清單.json>   # 吃複核過的清單，只執行剪片
python cli.py cut <影片> --out <資料夾>  # 指定輸出位置
```

## 其他指令

```bash
python cli.py shortform <影片> --title "標題" --cta "留言告訴我" --json
    # 做直式短影音：字幕 + 關鍵詞上色 + 大字卡 + 封面
    # --follow 追講者（單人才用，雙人會漏人）／--no-cards／--no-cover

python cli.py verify <成片> --frames --json
    # 檢查音畫字同步、字幕品質，並在每個刀口前後抽幀

python cli.py update --json
    # 更新規則包（門檻與判斷詞），不必重裝程式
    # --check-only 只看有沒有新版／--rollback 還原上一版

python cli.py ui
    # 開網頁介面（會佔住終端機直到使用者關閉）
```

完整指令：`doctor`、`setup`、`cut`、`shortform`、`verify`、`update`、`ui`、`version`。

## 重要行為，先知道再動手

**這件事很慢。** 一支 100 秒的影片，有 GPU 約 3-7 分鐘，沒有 GPU 可能 10-20 分鐘。
不要因為「沒有輸出」就以為當掉了——進度會持續印出來。**不要設短逾時。**

**判斷腦是選用的。** 沒有 `claude`/`codex` CLI、也沒有 API 金鑰時，
它只會剪靜音與卡頓，語意判斷停用。這是**降級不是失敗**，`ok` 仍為 `true`。

**它不會動使用者的系統。** 所有東西（FFmpeg、模型）都裝在專案資料夾的 `vendor/`，
不改 PATH、不需要管理員權限。你不需要、也不應該去改系統設定。

**輸出檔會放在影片旁邊**（除非指定 `--out`）。重跑會覆蓋。

## 出錯時

所有錯誤訊息都寫成「說明原因 + 下一步怎麼辦」的形式，直接照著做通常就能解。

| 症狀 | 處理 |
|---|---|
| `doctor` 說缺套件或 FFmpeg | `python bootstrap.py` |
| 「沒有辨識到任何語音」 | 確認影片有聲音、語言設定對（預設中文） |
| 「保留區間是空的」 | 偵測太兇，調寬 `rulepack/thresholds.json` 的門檻 |
| GPU 相關錯誤 | 它會自動退回 CPU，可忽略；想修就跑 `python bootstrap.py` |

## 不要做的事

- **不要改 `rulepack/` 裡的門檻來「讓結果好看」**——那些數值每個都對應一次真實的誤剪事故，
  改動前請先讀該檔案的註解。
- **不要跳過 `doctor` 直接跑 `cut`**——環境沒好會在辨識到一半才失敗，浪費好幾分鐘。
- **不要把 `vendor/` 或 `.venv/` 加進版控**（`.gitignore` 已排除）。

## 專案結構（需要改程式時）

```
bearcut/
  env/        平台偵測、FFmpeg 取得、GPU、自檢    ← 只用標準函式庫
  asr/        whisper（文字）+ Paraformer（時間）
  detect/     靜音・卡頓・重講・重複・破碎片段・安全閥
  llm/        判斷腦：本機 CLI 與 API 雙路
  pipeline.py 協調器 ← 想理解整個系統從這裡讀
rulepack/     門檻與 prompt（不寫死在程式碼裡）
CLAUDE.md     設計原則與已知地雷
```

**改動前務必讀 `CLAUDE.md` 的「⚠️ 改動前必讀」章節。** 那裡記錄了踩過的坑，
包括「看起來像 bug 但不能改」的地方。
