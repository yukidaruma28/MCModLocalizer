"""Anthropic Claude provider using the Messages API with forced tool use.

JSON output is enforced by declaring a single `emit_translations` tool and
setting `tool_choice` to require it. The tool's `input` is then serialized
back to JSON so the existing translation_batch parser (which expects
`{"items": [...]}`) handles it without modification.
"""
from __future__ import annotations

import json
from typing import Callable, Optional, Tuple

from ..usage import usage_from_response
from .base import ProviderResponse, _http_post_with_retry


_TOOL_NAME = "emit_translations"
_TOOL_DEFINITION = {
    "name": _TOOL_NAME,
    "description": "Emit Japanese translations for the input array, in the same order and count.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "required": ["items"],
    },
}


class ClaudeProvider:
    name = "claude"
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
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": model,
            "max_tokens": 16384,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
            "tools": [_TOOL_DEFINITION],
            "tool_choice": {"type": "tool", "name": _TOOL_NAME},
        }
        print(f"--- [DEBUG] SEND User (Claude) ---\n{user_text}\n-------------------------")
        resp = _http_post_with_retry(
            url,
            headers,
            body,
            retryable_status=(408, 409, 429, 500, 502, 503, 504, 529),
            immediate_failure_status=(400, 401, 403, 404, 413),
            log_fn=log_fn,
        )

        usage = usage_from_response(resp)

        # Surface max_tokens truncation as a retryable failure so the caller's
        # subset retry kicks in.
        stop_reason = resp.get("stop_reason")
        if stop_reason == "max_tokens":
            raise RuntimeError("Claude response truncated by max_tokens")

        text = ""
        for block in resp.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == _TOOL_NAME:
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    text = json.dumps(tool_input, ensure_ascii=False)
                break

        print(f"--- [DEBUG] RECV Assistant (Claude) ---\n{text}\n-----------------------------")
        return ProviderResponse(text=text or "", usage=usage)


__all__ = ["ClaudeProvider"]
