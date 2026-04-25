"""Gemini provider via the OpenAI-compatible Generative Language endpoint."""
from __future__ import annotations

from typing import Callable, Optional, Tuple

from ..usage import usage_from_response
from .base import ProviderResponse, _http_post_with_retry


_RESPONSE_FORMAT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "translation_result",
        "schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


class GeminiProvider:
    name = "gemini"
    default_models: Tuple[str, ...] = ("gemini-2.5-flash", "gemini-2.5-flash-lite")

    def chat_for_translation(
        self,
        api_key: str,
        model: str,
        system: str,
        user_text: str,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> ProviderResponse:
        url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            "response_format": _RESPONSE_FORMAT_SCHEMA,
        }
        print(f"--- [DEBUG] SEND User (Gemini) ---\n{user_text}\n-------------------------")
        resp = _http_post_with_retry(
            url,
            headers,
            body,
            retryable_status=(429, 500, 502, 503),
            immediate_failure_status=(400, 401, 403),
            log_fn=log_fn,
        )
        usage = usage_from_response(resp)
        content = ""
        if "choices" in resp and resp["choices"]:
            content = resp["choices"][0].get("message", {}).get("content", "") or ""
        print(f"--- [DEBUG] RECV Assistant (Gemini) ---\n{content}\n-----------------------------")
        return ProviderResponse(text=content or "", usage=usage)


__all__ = ["GeminiProvider"]
