# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""判斷腦：偵測、選擇、呼叫。

上層偵測層只需要這樣用，完全不必知道背後是誰：

    from bearcut.llm import get_llm
    llm = get_llm()
    if llm.available():
        result = llm.complete_json(prompt)

## 選擇順序（沒有任何設定時）

1. **明確指定** —— `BEARCUT_LLM` 環境變數或 config 的 `llm_backend`
2. **本機 CLI** —— 免金鑰，使用者裝過就能用，對「白癡都要能會用」最友善
3. **HTTP API** —— 有金鑰才會被選到
4. **都沒有** —— 回 NullProvider，降級成只剪靜音（不是失敗）

刻意讓 CLI 排在 API 前面：既然使用者已經有可用的免費判斷腦，就不該還去燒他的 API 額度。
"""

import os
from typing import List, Optional, Tuple

from .api import API_PROVIDERS, AnthropicApi, GeminiApi, OpenAiApi, OpenAiCompatible
from .base import (FAST, STRONG, LLMError, LLMUnavailable, NullProvider,
                   Provider, strip_json_fence)
from .cli import CLI_PROVIDERS, ClaudeCli, CodexCli, GeminiCli

__all__ = [
    "get_llm", "detect_all", "available_names",
    "Provider", "NullProvider", "LLMError", "LLMUnavailable",
    "FAST", "STRONG", "strip_json_fence",
]

# 使用者可用 BEARCUT_LLM 或 config["llm_backend"] 指定
_BY_NAME = {
    "claude": ClaudeCli, "claude_cli": ClaudeCli,
    "codex": CodexCli, "codex_cli": CodexCli,
    "gemini_cli": GeminiCli,
    "anthropic": AnthropicApi,
    "openai": OpenAiApi,
    "gemini": GeminiApi,
    "compatible": OpenAiCompatible, "ollama": OpenAiCompatible,
}

_cached: Optional[Provider] = None


def _build(cls) -> Optional[Provider]:
    """建立 provider；建構失敗（缺環境變數等）就當作不可用，不要讓偵測整個炸掉。"""
    try:
        return cls()
    except Exception:
        return None


def detect_all() -> List[Tuple[Provider, bool]]:
    """列出所有後端與其可用性。給 doctor 顯示用。"""
    out = []
    for cls in CLI_PROVIDERS + API_PROVIDERS:
        p = _build(cls)
        if p is not None:
            try:
                out.append((p, p.available()))
            except Exception:
                out.append((p, False))
    return out


def available_names() -> List[str]:
    return [p.name for p, ok in detect_all() if ok]


def get_llm(cfg: Optional[dict] = None, refresh: bool = False) -> Provider:
    """取得判斷腦。永遠會回一個物件——沒有可用後端時回 NullProvider。

    呼叫端應該先問 `available()`，而不是用 try/except 包住整段流程：
    沒有判斷腦是**預期內的降級路徑**，不是例外狀況。
    """
    global _cached
    if _cached is not None and not refresh:
        return _cached

    cfg = cfg or {}
    # 1) 明確指定
    want = (os.environ.get("BEARCUT_LLM") or cfg.get("llm_backend") or "").strip().lower()
    if want:
        cls = _BY_NAME.get(want)
        if cls is None:
            raise LLMError(
                f"不認得的判斷腦後端 '{want}'。可用值："
                + "、".join(sorted(set(_BY_NAME))))
        p = _build(cls)
        if p is None or not p.available():
            raise LLMError(
                f"指定了 '{want}' 但它在這台機器上不可用。"
                f"{'請確認該 CLI 已安裝且在 PATH 中。' if 'cli' in want or want in ('claude','codex') else '請確認 API 金鑰已設定。'}")
        _cached = p
        return p

    # 2) 自動：CLI 優先（免金鑰），再 API
    for cls in CLI_PROVIDERS + API_PROVIDERS:
        p = _build(cls)
        if p is not None and p.available():
            _cached = p
            return p

    # 3) 都沒有 → 降級
    _cached = NullProvider()
    return _cached
