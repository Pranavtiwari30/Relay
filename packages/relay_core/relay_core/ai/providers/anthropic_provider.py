"""Claude via the official Anthropic SDK (optional dependency, imported lazily).

Notes that shape this adapter (current-generation Claude models):

* The assistant turn is appended back **unchanged** (``response.content``),
  including thinking blocks. The history is append-only: never rebuilt or edited.
* No sampling parameters, no disabled thinking and no forced ``tool_choice`` —
  current models reject all three. Depth is controlled with ``effort`` only.
* ``stop_reason`` is checked before content is read; a ``refusal`` becomes a
  ``ProviderError`` so the investigator falls back.
* Server-side refusal fallback is opted into (``fallbacks="default"``), so a
  safety-classifier false positive is retried on Anthropic's recommended model
  inside the same request.
"""

from __future__ import annotations

from typing import Any

from relay_core.ai.providers.base import (
    ErrorKind,
    ModelTurn,
    ProviderError,
    ToolCall,
    ToolDefinition,
    ToolResult,
    Usage,
)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class _AnthropicSession:
    def __init__(
        self,
        provider: AnthropicProvider,
        system: str,
        user: str,
        tools: list[ToolDefinition],
        max_output_tokens: int,
    ) -> None:
        self._provider = provider
        self._system = system
        self._tools = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        self._max_tokens = max_output_tokens
        self._messages: list[dict[str, Any]] = [{"role": "user", "content": user}]

    def send(
        self,
        *,
        tool_results: list[ToolResult] | None = None,
        user_text: str | None = None,
        timeout: float,
    ) -> ModelTurn:
        import anthropic

        if tool_results or user_text:
            # All tool results for a turn go back in one user message.
            blocks: list[dict[str, Any]] = [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.content,
                    "is_error": r.is_error,
                }
                for r in tool_results or []
            ]
            if user_text:
                blocks.append({"type": "text", "text": user_text})
            self._messages.append({"role": "user", "content": blocks})
        try:
            response = self._provider.client.with_options(timeout=timeout).beta.messages.create(
                model=self._provider.model,
                max_tokens=self._max_tokens,
                system=self._system,
                tools=self._tools,
                messages=self._messages,
                output_config={"effort": self._provider.effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.APITimeoutError as exc:
            raise ProviderError("timeout", f"no response within {timeout:g}s") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("rate_limited", "Anthropic API rate limit reached") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ProviderError("auth", "Anthropic API rejected the credential") from exc
        except anthropic.APIStatusError as exc:
            kind: ErrorKind = "unavailable" if exc.status_code >= 500 else "bad_response"
            raise ProviderError(kind, f"Anthropic API error (HTTP {exc.status_code})") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("unavailable", "could not reach the Anthropic API") from exc

        usage = Usage(
            input_tokens=sum(
                getattr(response.usage, name, 0) or 0
                for name in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            ),
            output_tokens=getattr(response.usage, "output_tokens", 0) or 0,
        )
        if response.stop_reason == "refusal":
            raise ProviderError("refused", "the model declined this request")
        if response.stop_reason == "max_tokens":
            raise ProviderError("truncated", "model output hit the token limit")

        self._messages.append({"role": "assistant", "content": response.content})
        text_parts = [block.text for block in response.content if block.type == "text"]
        calls = [
            ToolCall(block.id, block.name, block.input)
            for block in response.content
            if block.type == "tool_use"
        ]
        return ModelTurn(
            text="\n".join(part for part in text_parts if part.strip()) or None,
            tool_calls=calls,
            model=response.model,
            usage=usage,
        )


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self, api_key: str, model: str, *, effort: str = "medium", client: Any = None
    ) -> None:
        self.model = model
        self.effort = effort
        if client is None:
            import anthropic

            # Retries are handled one level up by provider fallback, within a
            # single overall deadline.
            client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        self.client = client

    def start(
        self, *, system: str, user: str, tools: list[ToolDefinition], max_output_tokens: int
    ) -> _AnthropicSession:
        return _AnthropicSession(self, system, user, tools, max_output_tokens)
