"""Providers that speak plain JSON over HTTP: local Ollama and OpenAI-compatible endpoints.

Both base URLs come from operator configuration (environment), never from
workspace data, so they are not subject to the destination SSRF policy.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from relay_core.ai.providers.base import (
    ModelTurn,
    ProviderError,
    ToolCall,
    ToolDefinition,
    ToolResult,
    Usage,
)


def _function_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def _post(
    client: httpx.Client, url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    try:
        response = client.post(url, json=body, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", f"no response within {timeout:g}s") from exc
    except httpx.TransportError as exc:
        raise ProviderError(
            "unavailable", f"could not reach the model endpoint ({type(exc).__name__})"
        ) from exc
    if response.status_code in (401, 403):
        raise ProviderError(
            "auth", f"model endpoint rejected the credential (HTTP {response.status_code})"
        )
    if response.status_code == 429:
        raise ProviderError("rate_limited", "model endpoint is rate limiting requests")
    if response.status_code >= 500:
        raise ProviderError("unavailable", f"model endpoint error (HTTP {response.status_code})")
    if response.status_code >= 400:
        raise ProviderError(
            "bad_response", f"model endpoint rejected the request (HTTP {response.status_code})"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError("bad_response", "model endpoint returned a non-JSON body") from exc
    if not isinstance(data, dict):
        raise ProviderError("bad_response", "model endpoint returned an unexpected body")
    return data


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class _OllamaSession:
    def __init__(
        self,
        provider: OllamaProvider,
        system: str,
        user: str,
        tools: list[ToolDefinition],
        max_output_tokens: int,
    ) -> None:
        self._provider = provider
        self._tools = _function_tools(tools)
        self._max_output_tokens = max_output_tokens
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        self._calls = 0

    def send(
        self,
        *,
        tool_results: list[ToolResult] | None = None,
        user_text: str | None = None,
        timeout: float,
    ) -> ModelTurn:
        for result in tool_results or []:
            self._messages.append(
                {"role": "tool", "tool_name": result.name, "content": result.content}
            )
        if user_text:
            self._messages.append({"role": "user", "content": user_text})
        data = _post(
            self._provider.client,
            f"{self._provider.base_url}/api/chat",
            {
                "model": self._provider.model,
                "messages": self._messages,
                "tools": self._tools,
                "stream": False,
                "options": {"temperature": 0, "num_predict": self._max_output_tokens},
            },
            {},
            timeout,
        )
        message = data.get("message")
        if not isinstance(message, dict):
            raise ProviderError("bad_response", "Ollama response has no message")
        self._messages.append(message)
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") if isinstance(raw, dict) else None
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise ProviderError("bad_response", "Ollama returned a malformed tool call")
            self._calls += 1
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = None
            calls.append(
                ToolCall(str(raw.get("id") or f"call_{self._calls}"), function["name"], arguments)
            )
        if data.get("done_reason") == "length" and not calls:
            raise ProviderError("truncated", "model output hit the token limit")
        content = message.get("content")
        return ModelTurn(
            text=content if isinstance(content, str) and content.strip() else None,
            tool_calls=calls,
            model=str(data.get("model") or self._provider.model),
            usage=Usage(_int(data.get("prompt_eval_count")), _int(data.get("eval_count"))),
        )


class OllamaProvider:
    """Local models via Ollama's native chat API. No API key, no per-token cost."""

    name = "ollama"

    def __init__(
        self, base_url: str, model: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.Client(transport=transport, trust_env=False)

    def start(
        self, *, system: str, user: str, tools: list[ToolDefinition], max_output_tokens: int
    ) -> _OllamaSession:
        return _OllamaSession(self, system, user, tools, max_output_tokens)


class _OpenAICompatSession:
    def __init__(
        self,
        provider: OpenAICompatProvider,
        system: str,
        user: str,
        tools: list[ToolDefinition],
        max_output_tokens: int,
    ) -> None:
        self._provider = provider
        self._tools = _function_tools(tools)
        self._max_output_tokens = max_output_tokens
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def send(
        self,
        *,
        tool_results: list[ToolResult] | None = None,
        user_text: str | None = None,
        timeout: float,
    ) -> ModelTurn:
        for result in tool_results or []:
            self._messages.append(
                {"role": "tool", "tool_call_id": result.call_id, "content": result.content}
            )
        if user_text:
            self._messages.append({"role": "user", "content": user_text})
        data = _post(
            self._provider.client,
            f"{self._provider.base_url}/chat/completions",
            {
                "model": self._provider.model,
                "messages": self._messages,
                "tools": self._tools,
                "tool_choice": "auto",
                "max_tokens": self._max_output_tokens,
                "temperature": 0,
            },
            {"Authorization": f"Bearer {self._provider.api_key}"},
            timeout,
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("bad_response", "response has no choices[0].message") from exc
        if not isinstance(message, dict):
            raise ProviderError("bad_response", "response message is malformed")
        self._messages.append(message)
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") if isinstance(raw, dict) else None
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise ProviderError("bad_response", "model returned a malformed tool call")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (ValueError, TypeError):
                arguments = None  # surfaced to the model as an invalid-arguments tool result
            calls.append(ToolCall(str(raw.get("id") or ""), function["name"], arguments))
        if choice.get("finish_reason") == "length" and not calls:
            raise ProviderError("truncated", "model output hit the token limit")
        if choice.get("finish_reason") == "content_filter":
            raise ProviderError("refused", "the provider's content filter stopped the response")
        raw_usage = data.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        content = message.get("content")
        return ModelTurn(
            text=content if isinstance(content, str) and content.strip() else None,
            tool_calls=calls,
            model=str(data.get("model") or self._provider.model),
            usage=Usage(_int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens"))),
        )


class OpenAICompatProvider:
    """Any hosted endpoint implementing the OpenAI chat-completions wire format."""

    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.client = httpx.Client(transport=transport, trust_env=False)

    def start(
        self, *, system: str, user: str, tools: list[ToolDefinition], max_output_tokens: int
    ) -> _OpenAICompatSession:
        return _OpenAICompatSession(self, system, user, tools, max_output_tokens)
