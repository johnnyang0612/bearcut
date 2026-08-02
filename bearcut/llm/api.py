# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""HTTP API 判斷腦（需要金鑰）。

給沒裝 CLI、或想跑在伺服器上的使用者。支援 Anthropic / OpenAI / Gemini，
以及任何 **OpenAI 相容端點**（Ollama、LM Studio、OpenRouter、vLLM…）——
後者讓使用者可以完全本地離線跑，或自帶偏好的供應商。

刻意用標準函式庫的 urllib 而非各家 SDK：`pip install bearcut` 要保持輕量，
為了幾個 HTTP POST 拉進三個 SDK 不划算。

**決定性**：全部 temperature=0；支援 seed 的（OpenAI、Gemini）另外固定 seed。
Anthropic 沒有 seed 參數，temperature=0 已足夠穩定。
"""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from .base import FAST, STRONG, LLMError, Provider

SEED = 42
MAX_TOKENS = 8192
_RETRY_STATUS = (429, 500, 502, 503, 504)


def _post(url: str, headers: dict, body: dict, timeout: int, tries: int = 3) -> dict:
    """POST JSON 並在可重試的錯誤上退避重試。

    429/5xx 是暫時性的（額度、供應商抖動），值得重試；4xx 其他多半是設定錯誤，
    重試只是浪費時間，直接把可讀的原因拋出去。
    """
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = str(e)
            if e.code in (401, 403):
                raise LLMError("API 金鑰無效或沒有權限，請檢查金鑰設定。") from e
            if e.code in _RETRY_STATUS and attempt < tries - 1:
                time.sleep(5 * (attempt + 1))
                last = f"HTTP {e.code}: {detail}"
                continue
            raise LLMError(f"API 呼叫失敗（HTTP {e.code}）：{detail}") from e
        except (TimeoutError, OSError, urllib.error.URLError) as e:
            last = f"連線失敗：{e}"
            if attempt < tries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise LLMError(f"無法連線到 API：{e}") from e
    raise LLMError(f"API 重試多次仍失敗：{last}")


class ApiProvider(Provider):
    kind = "api"
    needs_key = True
    env_keys: tuple = ()
    models = {FAST: "", STRONG: ""}

    def __init__(self, api_key: Optional[str] = None, models: Optional[dict] = None):
        self.api_key = (api_key or self._key_from_env() or "").strip()
        if models:
            self.models = {**self.models, **models}

    def _key_from_env(self) -> str:
        for k in self.env_keys:
            v = os.environ.get(k)
            if v:
                return v
        return ""

    def available(self) -> bool:
        return bool(self.api_key)

    def _model(self, tier: str) -> str:
        return self.models.get(tier) or self.models.get(FAST) or ""


class AnthropicApi(ApiProvider):
    """Anthropic Messages API。

    模型分級與 CLI 後端一致：一般判斷用 Haiku（快、便宜約 4 倍），
    只有校字疑義二審提級到 Sonnet。Anthropic 沒有 seed 參數，靠 temperature=0 取得穩定性。
    """

    name = "Anthropic API"
    env_keys = ("ANTHROPIC_API_KEY",)
    models = {
        FAST: "claude-haiku-4-5-20251001",
        STRONG: "claude-sonnet-5",
    }

    def complete(self, prompt: str, tier: str = FAST, timeout: int = 600) -> str:
        data = _post(
            "https://api.anthropic.com/v1/messages",
            {
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            {
                "model": self._model(tier),
                "max_tokens": MAX_TOKENS,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout,
        )
        try:
            return "".join(b.get("text", "") for b in data["content"]
                           if b.get("type") == "text").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Anthropic 回應格式非預期：{str(data)[:200]}") from e


class OpenAiApi(ApiProvider):
    name = "OpenAI API"
    env_keys = ("OPENAI_API_KEY",)
    models = {FAST: "gpt-4o-mini", STRONG: "gpt-4o"}
    base_url = "https://api.openai.com/v1"

    def complete(self, prompt: str, tier: str = FAST, timeout: int = 600) -> str:
        data = _post(
            f"{self.base_url}/chat/completions",
            {"content-type": "application/json",
             "authorization": f"Bearer {self.api_key}"},
            {
                "model": self._model(tier),
                "temperature": 0,
                "seed": SEED,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout,
        )
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"OpenAI 回應格式非預期：{str(data)[:200]}") from e


class OpenAiCompatible(OpenAiApi):
    """任何 OpenAI 相容端點：Ollama、LM Studio、OpenRouter、vLLM…

    設 `BEARCUT_LLM_BASE_URL`（例：http://localhost:11434/v1）與 `BEARCUT_LLM_MODEL` 即可。
    本地端點通常不驗金鑰，所以這裡沒有金鑰也算可用。
    """

    name = "OpenAI 相容端點"
    env_keys = ("BEARCUT_LLM_API_KEY", "OPENAI_API_KEY")

    def __init__(self, api_key: Optional[str] = None, models: Optional[dict] = None,
                 base_url: Optional[str] = None):
        super().__init__(api_key, models)
        self.base_url = (base_url or os.environ.get("BEARCUT_LLM_BASE_URL") or "").rstrip("/")
        m = os.environ.get("BEARCUT_LLM_MODEL")
        if m:
            self.models = {FAST: m, STRONG: m}

    def available(self) -> bool:
        # 本地端點多半不需要金鑰，有 base_url 就當作可用
        return bool(self.base_url and self._model(FAST))

    def describe(self) -> str:
        return f"{self.name}（{self.base_url}）"


class GeminiApi(ApiProvider):
    """Google Gemini。

    依序嘗試模型清單：新模型有時尚未對某些金鑰開放，往下退回比整個失敗好。
    seed 固定，確保同輸入同輸出。
    """

    name = "Gemini API"
    env_keys = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    models = {FAST: "gemini-2.5-flash", STRONG: "gemini-2.5-pro"}
    fallbacks = ["gemini-2.5-flash", "gemini-2.0-flash"]

    def complete(self, prompt: str, tier: str = FAST, timeout: int = 600) -> str:
        chain = [self._model(tier)] + [m for m in self.fallbacks if m != self._model(tier)]
        last = None
        for model in chain:
            url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent")
            try:
                data = _post(
                    url,
                    {"content-type": "application/json", "x-goog-api-key": self.api_key},
                    {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0, "topP": 1, "topK": 1, "seed": SEED,
                        },
                    },
                    timeout,
                )
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except LLMError as e:
                last = e
                continue
            except (KeyError, IndexError, TypeError) as e:
                last = LLMError(f"Gemini 回應格式非預期：{str(data)[:200]}")
                continue
        raise last or LLMError("Gemini 所有模型都失敗")


# 偵測順序：本地相容端點優先（使用者刻意設了就是想用它），再來是各家雲端 API
API_PROVIDERS = [OpenAiCompatible, AnthropicApi, OpenAiApi, GeminiApi]
