"""LLM provider abstraction shared by Gemini and Claude implementations."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Protocol, Tuple

from ..usage import UsageStats


@dataclass
class ProviderResponse:
    text: str
    usage: UsageStats


class Provider(Protocol):
    name: str
    default_models: Tuple[str, ...]

    def chat_for_translation(
        self,
        api_key: str,
        model: str,
        system: str,
        user_text: str,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> ProviderResponse:
        ...


def _http_post_with_retry(
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    *,
    retryable_status: Iterable[int],
    immediate_failure_status: Iterable[int],
    log_fn: Optional[Callable[[str], None]] = None,
    max_retries: int = 5,
    base_delay: float = 5.0,
) -> Dict[str, Any]:
    """POST JSON to url with retry. Returns parsed JSON dict.

    Mirrors the retry behavior previously hard-coded in translation_batch.py:
    exponential backoff on retryable HTTP status, immediate raise on the
    immediate-failure list, and retry on generic network exceptions.
    """
    retryable = set(retryable_status)
    immediate = set(immediate_failure_status)
    retry_delay = base_delay
    data_bytes = json.dumps(body).encode("utf-8")

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
            with urllib.request.urlopen(req) as response:
                resp_body = response.read().decode("utf-8")
            return json.loads(resp_body)

        except urllib.error.HTTPError as e:
            if e.code in immediate:
                print(f"--- [ERROR] Immediate failure HTTP {e.code}: {e.reason}")
                raise

            if e.code not in retryable:
                print(f"--- [ERROR] Unhandled HTTP {e.code}: {e.reason}")
                raise

            if attempt == max_retries - 1:
                print(f"--- [ERROR] Retry limit exceeded for HTTP {e.code}.")
                raise

            msg = (
                f"[WARN] HTTP {e.code} ({e.reason}). Waiting {retry_delay:.2f}s... "
                f"(Attempt {attempt + 1}/{max_retries})"
            )
            print(f"--- {msg}")
            if log_fn:
                log_fn(msg)
            time.sleep(retry_delay)
            retry_delay *= 2

        except Exception as e:
            if attempt == max_retries - 1:
                raise
            msg = f"[WARN] Error: {e}. Retrying in {retry_delay}s..."
            print(f"--- {msg}")
            if log_fn:
                log_fn(msg)
            time.sleep(retry_delay)
            retry_delay *= 2

    raise RuntimeError("Unreachable: retry loop exited without return or raise")


def get_provider(name: str) -> Provider:
    """Resolve a provider by short name ('gemini', 'claude', 'claude_sdk')."""
    key = (name or "").strip().lower()
    if key in ("", "gemini"):
        from .gemini import GeminiProvider

        return GeminiProvider()
    if key == "claude":
        from .claude import ClaudeProvider

        return ClaudeProvider()
    if key == "claude_sdk":
        from .claude_sdk import ClaudeSDKProvider

        return ClaudeSDKProvider()
    raise ValueError(f"Unknown LLM provider: {name!r}")


__all__ = [
    "Provider",
    "ProviderResponse",
    "_http_post_with_retry",
    "get_provider",
]
