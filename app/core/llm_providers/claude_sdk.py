"""Claude provider using the Claude Agent SDK with subscription auth.

Unlike the `claude` provider which calls the Anthropic Messages API with an
API key, this provider routes through the Claude Agent SDK so it inherits the
authentication of the locally-installed Claude Code CLI (Pro / Max
subscription). No `ANTHROPIC_API_KEY` is required at runtime.

Requirements:
- `claude-agent-sdk` Python package installed.
- Claude Code CLI installed and the user logged in (`claude login`).

JSON output is enforced by prompt engineering rather than a schema, because
the simple `query()` async iterator does not surface `output_format`. The
existing parser in `translation_batch._parse_list` tolerates wrapping
(code fences, lists, etc.) so this is sufficient in practice.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Callable, Optional, Tuple

from ..usage import UsageStats
from .base import ProviderResponse


_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "429",
    "quota",
    "usage limit",
    "5-hour",
    "weekly",
    "too many requests",
)


class RateLimitExceeded(RuntimeError):
    """Raised when the Claude subscription's rate limit is detected.

    Distinguished from generic errors so the UI can abort the entire batch
    instead of skipping just one Mod — once you hit the 5-hour or weekly
    window, every subsequent call will fail too.
    """


def _is_rate_limit_error(exc: BaseException) -> bool:
    msg = (str(exc) or "").lower()
    return any(p in msg for p in _RATE_LIMIT_PATTERNS)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    if not text:
        return text
    m = _FENCE_RE.match(text)
    if m:
        return m.group(1)
    return text


class ClaudeSDKProvider:
    name = "claude_sdk"
    default_models: Tuple[str, ...] = (
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-7",
    )

    def chat_for_translation(
        self,
        api_key: str,
        model: str,
        system: str,
        user_text: str,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> ProviderResponse:
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ResultMessage,
                TextBlock,
                query,
            )
        except ImportError as e:
            raise RuntimeError(
                "claude-agent-sdk が見つかりません。`pip install claude-agent-sdk` を "
                "実行し、Claude Code CLI のインストールとログインも済ませてください。"
            ) from e

        system_with_json = system + (
            '\n\n出力は必ず単一の JSON オブジェクト {"items": [<訳1>, <訳2>, ...]} のみで、'
            "前後にコードフェンス・説明・空行を一切付けないこと。"
        )

        options = ClaudeAgentOptions(
            system_prompt=system_with_json,
            model=model,
            max_turns=1,
            allowed_tools=[],
            permission_mode="bypassPermissions",
        )

        print(f"--- [DEBUG] SEND User (Claude SDK) ---\n{user_text}\n-------------------------")

        async def _run() -> Tuple[str, UsageStats]:
            text_parts: list[str] = []
            usage = UsageStats()
            async for msg in query(prompt=user_text, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    raw_usage = getattr(msg, "usage", None)
                    if raw_usage is None:
                        continue
                    if hasattr(raw_usage, "to_dict"):
                        try:
                            raw_usage = raw_usage.to_dict()
                        except Exception:
                            pass
                    data = raw_usage if isinstance(raw_usage, dict) else getattr(raw_usage, "__dict__", {})
                    def _i(*keys: str) -> int:
                        for k in keys:
                            if k in data and data[k] is not None:
                                try:
                                    return int(data[k])
                                except Exception:
                                    return 0
                        return 0
                    prompt = _i("input_tokens", "prompt_tokens")
                    completion = _i("output_tokens", "completion_tokens")
                    total = _i("total_tokens") or (prompt + completion)
                    usage = UsageStats(
                        prompt_tokens=prompt,
                        completion_tokens=completion,
                        total_tokens=total,
                    )
            return "".join(text_parts), usage

        def _invoke() -> Tuple[str, UsageStats]:
            try:
                return asyncio.run(_run())
            except RuntimeError as e:
                if "asyncio.run() cannot be called" in str(e):
                    loop = asyncio.new_event_loop()
                    try:
                        return loop.run_until_complete(_run())
                    finally:
                        loop.close()
                raise

        try:
            text, usage = _invoke()
        except Exception as e:
            if _is_rate_limit_error(e):
                # 5 時間 / 週次のレート制限はリトライしても数時間は復旧しない。
                # 待つよりユーザーに即時通知して中断させる方が時間と Mod 単位の
                # スキップを節約できる。RateLimitExceeded として上位へ伝搬。
                msg = (
                    "[STOP] Claude 定額プランのレート制限を検出しました。"
                    "翻訳を中断します。時間を空けて (通常 5 時間〜) 再実行してください。"
                )
                print(f"--- {msg}")
                if log_fn:
                    log_fn(msg)
                raise RateLimitExceeded(str(e)) from e
            raise

        text = _strip_code_fence(text).strip()
        print(f"--- [DEBUG] RECV Assistant (Claude SDK) ---\n{text}\n-----------------------------")
        return ProviderResponse(text=text, usage=usage)


__all__ = ["ClaudeSDKProvider", "RateLimitExceeded"]
