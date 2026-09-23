"""Tiny LiteLLM-compatible shim for DeltaBox slim workers.

This is not a synthetic LLM. completion() forwards the exact messages to the
OpenAI-compatible mock_llm_server already used by the replay harness. The point
is only to avoid importing real litellm and its large dependency graph inside
the checkpoint target.
"""
from __future__ import annotations

import json
import typing
import urllib.error
import urllib.request
from typing import Any


drop_params = True
telemetry = False
callbacks: list[Any] = []
success_callback: list[Any] = []
failure_callback: list[Any] = []
_async_success_callback: list[Any] = []
_async_failure_callback: list[Any] = []
input_callback: list[Any] = []
Type = typing.Type


class LiteLLMError(Exception):
    def __init__(self, message: str = "", **kwargs: Any) -> None:
        super().__init__(message)
        self.message = message
        self.llm_provider = kwargs.get("llm_provider", "")
        self.model = kwargs.get("model", "")
        self.status_code = int(kwargs.get("status_code", 0) or 0)
        self.litellm_debug_info = kwargs.get("litellm_debug_info", "")
        self.num_retries = kwargs.get("num_retries", 0)
        self.max_retries = kwargs.get("max_retries", 0)


class APIError(LiteLLMError):
    pass


class BadRequestError(LiteLLMError):
    pass


class NotFoundError(LiteLLMError):
    pass


class AuthenticationError(LiteLLMError):
    pass


class CustomLogger:
    pass


class InMemoryCache:
    pass


class _AttrDict(dict):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e


def _to_obj(value: Any) -> Any:
    if isinstance(value, dict):
        return _AttrDict({k: _to_obj(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_obj(v) for v in value]
    return value


def _base_url(api_base: str | None) -> str:
    if not api_base:
        raise APIError("api_base is required for slim litellm shim")
    base = api_base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def completion(
    *,
    model: str,
    messages: list[dict],
    max_tokens: int | None = None,
    temperature: float | None = None,
    metadata: dict | None = None,
    timeout: float | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    stop: list[str] | None = None,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    response_format: dict | None = None,
    request_timeout: float | None = None,
    **kwargs: Any,
) -> Any:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature
    if metadata is not None:
        payload["metadata"] = metadata
    if stop is not None:
        payload["stop"] = stop
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if response_format is not None:
        payload["response_format"] = response_format

    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _base_url(api_base),
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key or 'dummy'}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=request_timeout or timeout or 120.0) as resp:
            return _to_obj(json.loads(resp.read()))
    except urllib.error.HTTPError as e:
        raise APIError(str(e), model=model, status_code=e.code) from e
    except Exception as e:
        raise APIError(str(e), model=model) from e


def completion_cost(*args: Any, **kwargs: Any) -> float:
    return 0.0


def cost_per_token(*args: Any, **kwargs: Any) -> tuple[float, float]:
    return 0.0, 0.0


def token_counter(*, model: str | None = None, messages: list[dict] | None = None,
                  text: str | None = None, **kwargs: Any) -> int:
    if messages is not None:
        raw = json.dumps(messages, ensure_ascii=False)
    else:
        raw = text or ""
    return max(1, len(raw) // 4)
