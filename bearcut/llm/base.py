# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""判斷腦的共用介面與工具。

BearCut 的語意判斷（哪句是重講、哪個字聽錯、這一刀接得順不順）可以走兩條路：
- **本機 CLI**（claude / codex / gemini）：免金鑰，使用者裝過就能用
- **HTTP API**（Anthropic / OpenAI / Gemini / OpenAI 相容端點）：要金鑰，可跑在無 CLI 的機器

兩條路都實作同一個 `Provider.complete()`，上層偵測層完全不需要知道自己在跟誰講話。

## 兩個不可妥協的設計

**1. 決定性。** 所有呼叫一律 temperature=0、固定 seed。否則同一支影片跑兩次，
邊界判斷（這句算不算重講）會飄，造成「驗證時對、出片時錯」——這是最難查的一種 bug。

**2. 沒有判斷腦也要能跑。** 判斷腦缺席時只降級成「只剪靜音」，不是整個失敗。
靜音偵測是純訊號處理，不需要 LLM。
"""

import json
import re
from typing import Optional


class LLMError(RuntimeError):
    """判斷腦呼叫失敗。訊息一律寫成使用者看得懂、且說得出下一步怎麼辦。"""


class LLMUnavailable(LLMError):
    """找不到任何可用的判斷腦。上層應降級成只剪靜音，而不是中止。"""


# 模型分級：絕大多數判斷（這句是不是重講）用 FAST 就夠，快 ~10 倍也便宜 ~10 倍；
# 只有校字疑義二審這種需要上下文推理的才提級到 STRONG。
FAST = "fast"
STRONG = "strong"


def strip_json_fence(text: str) -> str:
    """把模型回的文字清成純 JSON。

    模型很愛做兩件事：用 ```json``` 把答案圍起來、在 JSON 前後加一句說明。
    這裡兩種都剝掉——下游一律 json.loads，多一個字就炸。
    """
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    # 抓第一個 { 或 [ 到最後一個 } 或 ]，去掉模型偶爾夾帶的前後說明句
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if starts:
        first = min(starts)
        last = max(t.rfind("}"), t.rfind("]"))
        if last > first:
            t = t[first:last + 1]
    return t


class Provider:
    """判斷腦後端的共同介面。"""

    name = "base"
    kind = "unknown"        # "cli" 或 "api"
    needs_key = False
    free = False            # 免金鑰（本機 CLI）

    def available(self) -> bool:
        """這個後端在這台機器上能不能用。偵測要便宜——會被 doctor 頻繁呼叫。"""
        raise NotImplementedError

    def complete(self, prompt: str, tier: str = FAST, timeout: int = 600) -> str:
        """送出 prompt，回傳純文字。實作必須是決定性的。"""
        raise NotImplementedError

    # --- 共用便利方法 ---

    def complete_json(self, prompt: str, tier: str = FAST, timeout: int = 600):
        """要求 JSON 回應並解析。模型偶爾會回不合法 JSON，這裡給明確的錯誤。"""
        raw = self.complete(prompt, tier=tier, timeout=timeout)
        cleaned = strip_json_fence(raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"{self.name} 回傳的不是合法 JSON（{e}）。"
                f"前 200 字：{cleaned[:200]}"
            ) from e

    def describe(self) -> str:
        """給 UI / doctor 顯示的一句白話。"""
        tag = "免金鑰" if self.free else "需要金鑰"
        return f"{self.name}（{tag}）"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"


class NullProvider(Provider):
    """沒有任何判斷腦時的佔位。

    刻意不在建立時就拋錯——上層要能先建立起來、看到 `available() is False`，
    然後自己決定降級成只剪靜音。真的被呼叫才拋，且訊息說明怎麼補。
    """

    name = "無判斷腦"
    kind = "none"

    def available(self) -> bool:
        return False

    def complete(self, prompt: str, tier: str = FAST, timeout: int = 600) -> str:
        raise LLMUnavailable(
            "沒有可用的判斷腦，語意判斷（重講／口吃／校字）無法執行。\n"
            "\n"
            "最省事的補法——裝一顆本機 CLI，用你現有的 Claude 付費方案登入，不必申請 API 金鑰：\n"
            "  Windows（PowerShell）：irm https://claude.ai/install.ps1 | iex\n"
            "  macOS / Linux：       curl -fsSL https://claude.ai/install.sh | bash\n"
            "  裝完執行一次 `claude` 完成瀏覽器登入，BearCut 下次就會自動偵測到。\n"
            "\n"
            "或設定環境變數 ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY。\n"
            "\n"
            "注意：如果你是用 AI agent（Claude Cowork、Codex…）在操作 BearCut，"
            "那個 agent 是**操作者**，不是判斷腦——BearCut 需要一顆自己能用程式呼叫的\n"
            "CLI 或 API，兩者是不同的東西，上面那一步還是要做。\n"
            "\n"
            "在此之前，BearCut 仍可只剪長靜音（純訊號處理，不需要判斷腦）。"
        )

    def describe(self) -> str:
        return "無（只能剪靜音）"
