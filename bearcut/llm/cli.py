# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""本機 CLI 判斷腦（免金鑰）。

使用者若已裝過 Claude Code 或 Codex，BearCut 直接借用，不必再申請 API 金鑰。
對「白癡都要能會用」而言這條路很關鍵——少一個要註冊、要付費、要貼金鑰的步驟。

## 三個從實戰換來的細節（改動前請先讀）

1. **prompt 一律走 stdin。** 逐字稿動輒上萬字，塞命令列參數會超過長度上限。
2. **cwd 切到中性暫存目錄。** 否則 CLI 會載入當前專案的 CLAUDE.md 與記憶，
   小模型被那些 context 干擾後會回閒聊而不是 JSON。實測 Haiku 在專案目錄下會這樣，
   換到乾淨目錄就穩定回 JSON。這是最容易誤判成「模型太笨」的坑。
3. **逾時要給足。** 長逐字稿的判斷可能跑好幾分鐘，逾時設太短會在大檔案上隨機失敗。
"""

import os
import shutil
import subprocess
import tempfile
from typing import List, Optional

from .base import FAST, STRONG, LLMError, Provider, strip_json_fence


class CliProvider(Provider):
    """以子行程呼叫本機 CLI 的共同實作。"""

    kind = "cli"
    free = True
    binary = ""
    models = {FAST: "", STRONG: ""}     # 分級 → 模型名；空字串＝用該 CLI 的預設

    def __init__(self, models: Optional[dict] = None):
        if models:
            self.models = {**self.models, **models}
        self._path = None

    # --- 偵測 ---

    def available(self) -> bool:
        if self._path is None:
            self._path = shutil.which(self.binary) or ""
        return bool(self._path)

    # --- 子類覆寫：組出命令 ---

    def _command(self, model: str) -> List[str]:
        raise NotImplementedError

    # --- 執行 ---

    def _isolated_cwd(self) -> Optional[str]:
        """中性工作目錄，避免 CLI 載入專案 context 污染判斷（見模組說明第 2 點）。"""
        d = os.path.join(tempfile.gettempdir(), "bearcut_llm_isolated")
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            return None

    def complete(self, prompt: str, tier: str = FAST, timeout: int = 600) -> str:
        if not self.available():
            raise LLMError(f"找不到 {self.binary} 指令")

        model = self.models.get(tier) or self.models.get(FAST) or ""
        cmd = self._command(model)
        timeout = max(int(timeout or 0), 600)

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=self._isolated_cwd(),
            )
        except FileNotFoundError as e:
            raise LLMError(
                f"找不到 {self.binary} 指令。若已安裝，請確認它在 PATH 中。"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise LLMError(
                f"{self.name} 逾時（超過 {timeout} 秒）。"
                f"影片很長時可以調高逾時，或改用 API 後端。"
            ) from e

        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or not out:
            err = (proc.stderr or "").strip()[:300]
            raise LLMError(
                f"{self.name} 執行失敗（結束碼 {proc.returncode}）"
                + (f"：{err}" if err else "。沒有輸出。")
            )
        return strip_json_fence(out)


class ClaudeCli(CliProvider):
    """Claude Code CLI。**此後端已實測驗證。**

    判斷「這句是不是重講」不需要最強模型：Haiku 快約 10 倍、便宜約 10 倍，品質足夠。
    只有校字疑義二審才提級到 Sonnet——那需要帶上下文推理。
    """

    name = "Claude CLI"
    binary = "claude"
    models = {
        FAST: "claude-haiku-4-5-20251001",
        STRONG: "claude-sonnet-5",
    }

    def _command(self, model: str) -> List[str]:
        cmd = ["claude", "-p", "--output-format", "text"]
        if model:
            cmd += ["--model", model]
        return cmd


class CodexCli(CliProvider):
    """OpenAI Codex CLI。

    ⚠️ **尚未實測**——開發機沒有安裝 codex，非互動模式的旗標是依官方文件撰寫、
    未經實跑驗證。若你手上有 codex 而這條路失敗，請開 issue 附上錯誤訊息，
    或先改用 API 後端。
    """

    name = "Codex CLI"
    binary = "codex"
    models = {FAST: "", STRONG: ""}     # 交給 codex 自己的預設

    def _command(self, model: str) -> List[str]:
        cmd = ["codex", "exec", "-"]    # "-" = 從 stdin 讀 prompt
        if model:
            cmd += ["--model", model]
        return cmd


class GeminiCli(CliProvider):
    """Google Gemini CLI。

    ⚠️ **尚未實測**——理由同 CodexCli。
    """

    name = "Gemini CLI"
    binary = "gemini"
    models = {FAST: "", STRONG: ""}

    def _command(self, model: str) -> List[str]:
        cmd = ["gemini", "-p"]          # prompt 由 stdin 供給
        if model:
            cmd += ["-m", model]
        return cmd


# 偵測順序：claude 排最前面，因為它是唯一經過實測的。
CLI_PROVIDERS = [ClaudeCli, CodexCli, GeminiCli]
