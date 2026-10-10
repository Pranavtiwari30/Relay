"""Wire-format and interface contracts: adapters, broker envelope, model providers, MCP.

These pin the shapes Relay exchanges with things it does not control, using
recorded-shape fakes so they run offline and deterministically.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp import Client

from relay_core.adapters.base import DeliveryRequest
from relay_core.adapters.demo_destination import DemoDestinationAdapter
from relay_core.adapters.http_destination import HttpDestinationAdapter
from relay_core.adapters.registry import AdapterRegistry, build_default_registry
from relay_core.ai.providers.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from relay_core.ai.providers.base import ProviderError, ToolDefinition, ToolResult
from relay_core.ai.providers.http_chat import OllamaProvider, OpenAICompatProvider
from relay_core.ai.tools import READ_ONLY_TOOLS
from relay_core.config import Settings
from relay_core.domain.retry import Disposition, FailureCategory
from relay_core.domain.urlsafety import UrlPolicy
from relay_core.errors import UnauthenticatedError
from relay_core.mcp_server import build_server
from relay_core.messaging.base import (
    DeliveryMessage,
    MalformedMessageError,
    PublishError,
    decode_push_envelope,
    encode_push_envelope,
)
from relay_core.messaging.publishers import PubSubPublisher
from relay_worker.internal_auth import InternalAuthenticator
from tests.support import Harness, order

TOOLS = [
    ToolDefinition(
        "get_event",
        "Read an event.",
        {
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
    )
]


def request(**overrides: Any) -> DeliveryRequest:
    values: dict[str, Any] = {
        "event_id": uuid.uuid4(), "workspace_id": uuid.uuid4(), "payload": {"order_id": "A-1", "note": "café"},
        "idempotency_key": "idem-1", "correlation_id": "corr-12345678", "attempt_number": 2, "timeout_seconds": 2.0,
    }  # fmt: skip
    values.update(overrides)
    return DeliveryRequest(**values)


# --- destination adapter contract -------------------------------------------


def adapter_for(
    handler: Any, *, addresses: list[str] | None = None, policy: UrlPolicy | None = None
) -> HttpDestinationAdapter:
    return HttpDestinationAdapter(
        policy or UrlPolicy(),
        resolver=lambda host, port: addresses or ["93.184.216.34"],
        transport=httpx.MockTransport(handler),
    )


def test_http_adapter_connects_to_the_pinned_address_but_keeps_host_and_sni() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(
            url=str(req.url),
            headers=dict(req.headers),
            body=req.content,
            sni=req.extensions.get("sni_hostname"),
        )
        return httpx.Response(201, json={"ok": True})

    adapter = adapter_for(handler)
    config = {
        "url": "https://api.example.com:8443/v1/orders?tenant=7",
        "headers": {"X-Tenant": "acme"},
        "auth": {"type": "header", "header_name": "X-Api-Key"},
    }
    result = adapter.deliver(config, "s3cret-value", request())

    assert (result.disposition, result.http_status) == (Disposition.SUCCESS, 201)
    # TCP goes to the address that was validated — no second DNS lookup to rebind.
    assert seen["url"] == "https://93.184.216.34:8443/v1/orders?tenant=7"
    # TLS verification and virtual hosting still use the real name.
    assert seen["sni"] == "api.example.com"
    assert seen["headers"]["host"] == "api.example.com:8443"
    assert seen["headers"]["x-api-key"] == "s3cret-value"
    assert seen["headers"]["x-tenant"] == "acme"
    assert seen["headers"]["idempotency-key"] == "idem-1"
    assert seen["headers"]["x-relay-attempt"] == "2"
    assert json.loads(seen["body"]) == {"order_id": "A-1", "note": "café"}
    # What is stored for operators has no query string and no credential.
    assert result.request_url == "https://api.example.com:8443/v1/orders"
    assert "s3cret-value" not in repr(result)


@pytest.mark.parametrize(
    ("auth", "secret", "header", "expected"),
    [
        ({"type": "bearer"}, "tok", "authorization", "Bearer tok"),
        (
            {"type": "basic", "username": "svc"},
            "pw",
            "authorization",
            "Basic " + base64.b64encode(b"svc:pw").decode(),
        ),
        ({"type": "header", "header_name": "X-Key"}, "k", "x-key", "k"),
    ],
)
def test_http_adapter_auth_schemes(
    auth: dict[str, str], secret: str, header: str, expected: str
) -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(req.headers)
        return httpx.Response(200)

    adapter_for(handler).deliver(
        {"url": "https://api.example.com/x", "auth": auth}, secret, request()
    )
    assert captured[header] == expected


def test_http_adapter_never_sends_to_a_host_that_resolves_privately() -> None:
    called = False

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    for addresses in (["10.0.0.5"], ["169.254.169.254"], ["93.184.216.34", "127.0.0.1"], ["::1"]):
        result = adapter_for(handler, addresses=addresses).deliver(
            {"url": "https://rebind.example.com/x"}, None, request()
        )
        assert (result.disposition, result.failure_category) == (
            Disposition.PERMANENT,
            FailureCategory.BLOCKED_DESTINATION,
        )
        assert "non-public" in (result.error_message or "")
    assert called is False


def test_http_adapter_classifies_every_failure_mode_without_raising() -> None:
    def raising(exc: Exception) -> Any:
        def handler(req: httpx.Request) -> httpx.Response:
            raise exc

        return handler

    cases: list[tuple[Any, Disposition, FailureCategory]] = [
        (raising(httpx.ConnectTimeout("t")), Disposition.RETRYABLE, FailureCategory.TIMEOUT),
        (raising(httpx.ReadTimeout("t")), Disposition.RETRYABLE, FailureCategory.TIMEOUT),
        (raising(httpx.ConnectError("refused")), Disposition.RETRYABLE, FailureCategory.TRANSPORT),
        (
            raising(httpx.RemoteProtocolError("bad")),
            Disposition.RETRYABLE,
            FailureCategory.INVALID_RESPONSE,
        ),
        (lambda r: httpx.Response(503), Disposition.RETRYABLE, FailureCategory.HTTP_5XX),
        (
            lambda r: httpx.Response(429, headers={"Retry-After": "12"}),
            Disposition.RETRYABLE,
            FailureCategory.HTTP_429,
        ),
        (
            lambda r: httpx.Response(422, json={"e": 1}),
            Disposition.PERMANENT,
            FailureCategory.HTTP_4XX,
        ),
        (lambda r: httpx.Response(401), Disposition.PERMANENT, FailureCategory.AUTH),
        (
            lambda r: httpx.Response(307, headers={"Location": "http://10.0.0.1/"}),
            Disposition.PERMANENT,
            FailureCategory.REDIRECT,
        ),
    ]
    for handler, disposition, category in cases:
        result = adapter_for(handler).deliver({"url": "https://api.example.com/x"}, None, request())
        assert (result.disposition, result.failure_category) == (disposition, category)
        assert result.error_message
    limited = adapter_for(cases[5][0]).deliver(
        {"url": "https://api.example.com/x"}, None, request()
    )
    assert limited.retry_after_seconds == 12


def test_http_adapter_bounds_how_much_of_a_response_it_reads() -> None:
    adapter = HttpDestinationAdapter(
        UrlPolicy(), max_response_bytes=2048, resolver=lambda h, p: ["93.184.216.34"],
        transport=httpx.MockTransport(lambda r: httpx.Response(500, content=b"E" * 5_000_000)),
    )  # fmt: skip
    result = adapter.deliver({"url": "https://api.example.com/x"}, None, request())
    assert result.http_status == 500
    assert len(result.response_summary or "") < 2200


def test_http_adapter_reports_configuration_problems_as_permanent() -> None:
    adapter = adapter_for(lambda r: httpx.Response(200))
    missing_secret = adapter.deliver(
        {"url": "https://api.example.com/x", "auth": {"type": "bearer"}}, None, request()
    )
    invalid = adapter.deliver({"url": 5}, None, request())
    for result in (missing_secret, invalid):
        assert (result.disposition, result.failure_category) == (
            Disposition.PERMANENT,
            FailureCategory.CONFIGURATION,
        )


def test_demo_adapter_targets_only_the_configured_demo_host() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(201, json={"status": "accepted"})

    adapter = DemoDestinationAdapter(
        "http://127.0.0.1:8000", transport=httpx.MockTransport(handler)
    )
    req = request()
    assert adapter.deliver({}, "demo-token", req).disposition is Disposition.SUCCESS
    assert str(seen[0].url) == f"http://127.0.0.1:8000/demo/fulfillment/{req.workspace_id}/orders"
    assert seen[0].headers["authorization"] == "Bearer demo-token"
    assert adapter.validate_config({}, has_secret=True) == []
    assert (
        adapter.validate_config({"url": "https://elsewhere.example"}, has_secret=True)[0].code
        == "invalid_config"
    )
    assert adapter.validate_config({}, has_secret=False)[0].code == "secret_required"


def test_every_registered_destination_adapter_honours_the_same_contract() -> None:
    """New adapters are picked up automatically and must satisfy the protocol."""
    settings = Settings(_env_file=None, env="test", demo_destination_base_url="http://127.0.0.1:1")  # type: ignore[call-arg]
    registry = build_default_registry(settings)
    assert registry.source_types == ["webhook"]
    assert registry.destination_types == ["demo", "http"]
    for name in registry.destination_types:
        adapter = registry.destination(name)
        assert adapter.adapter_type == name
        assert isinstance(
            adapter.validate_config({"definitely": "wrong", "url": 1}, has_secret=False), list
        )
        # deliver() must return a classified result — never raise — even when unreachable.
        result = adapter.deliver(
            {"url": "http://127.0.0.1:1/x"}, "secret", request(timeout_seconds=0.5)
        )
        assert result.disposition in (Disposition.RETRYABLE, Disposition.PERMANENT)
        assert result.failure_category is not None
    with pytest.raises(ValueError, match="already registered"):
        registry.register_destination(registry.destination("http"))
    fresh = AdapterRegistry()
    with pytest.raises(Exception, match="unknown destination adapter"):
        fresh.destination("http")


# --- broker contract --------------------------------------------------------


def test_delivery_message_round_trips_through_a_pubsub_push_envelope() -> None:
    message = DeliveryMessage(
        uuid.uuid4(), uuid.uuid4(), cycle=2, attempt=3, correlation_id="corr-abcdefgh"
    )
    envelope = encode_push_envelope(
        message.to_data(),
        message.attributes(),
        message_id="m-1",
        subscription="projects/p/subscriptions/s",
        delivery_attempt=4,
    )
    # Exactly the documented Pub/Sub push shape.
    assert set(envelope) == {"message", "subscription", "deliveryAttempt"}
    assert set(envelope["message"]) == {"data", "attributes", "messageId"}
    assert json.loads(base64.b64decode(envelope["message"]["data"])) == message.to_data()
    decoded = decode_push_envelope(envelope)
    assert (decoded.message_id, decoded.delivery_attempt) == ("m-1", 4)
    assert DeliveryMessage.from_data(decoded.data) == message
    # Messages carry identifiers only — never payloads.
    assert set(message.to_data()) == {
        "v",
        "event_id",
        "workspace_id",
        "cycle",
        "attempt",
        "correlation_id",
    }


def test_real_pubsub_envelope_fields_are_tolerated() -> None:
    real = {
        "message": {
            "data": base64.b64encode(b'{"v":1}').decode(),
            "messageId": "136969346945",
            "message_id": "136969346945",
            "publishTime": "2026-03-01T10:00:00.000Z",
            "publish_time": "2026-03-01T10:00:00.000Z",
            "orderingKey": "",
        },
        "subscription": "projects/demo/subscriptions/relay-deliveries-push",
    }
    decoded = decode_push_envelope(real)
    assert (decoded.data, decoded.attributes, decoded.delivery_attempt) == ({"v": 1}, {}, None)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"message": {}},
        {"message": {"data": "***"}},
        {"message": {"data": base64.b64encode(b"not json").decode()}},
        [],
        None,
        {"message": {"data": "e30=", "attributes": [1]}},
    ],
)
def test_malformed_envelopes_raise_one_exception_type(body: Any) -> None:
    with pytest.raises(MalformedMessageError):
        decode_push_envelope(body)


@pytest.mark.parametrize(
    "data",
    [None, [], {"v": 2}, {"v": 1}, {"v": 1, "event_id": "x", "workspace_id": "y", "cycle": 1, "attempt": 1, "correlation_id": "c"},
     {"v": 1, "event_id": str(uuid.uuid4()), "workspace_id": str(uuid.uuid4()), "cycle": True, "attempt": 1, "correlation_id": "c"},
     {"v": 1, "event_id": str(uuid.uuid4()), "workspace_id": str(uuid.uuid4()), "cycle": 1, "attempt": 0, "correlation_id": "c"},
     {"v": 1, "event_id": str(uuid.uuid4()), "workspace_id": str(uuid.uuid4()), "cycle": 1, "attempt": 1, "correlation_id": ""}],
)  # fmt: skip
def test_malformed_delivery_messages_are_rejected(data: Any) -> None:
    with pytest.raises(MalformedMessageError):
        DeliveryMessage.from_data(data)


def test_pubsub_publisher_only_returns_after_the_broker_confirms() -> None:
    published: list[tuple[str, bytes, dict[str, str]]] = []

    class Future:
        def __init__(self, outcome: Any) -> None:
            self.outcome = outcome
            self.timeout: float | None = None

        def result(self, timeout: float) -> Any:
            self.timeout = timeout
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    class Client_:
        outcome: Any = "msg-123"
        last: Future | None = None

        def topic_path(self, project: str, topic: str) -> str:
            return f"projects/{project}/topics/{topic}"

        def publish(self, topic: str, data: bytes, **attributes: str) -> Future:
            published.append((topic, data, attributes))
            self.last = Future(self.outcome)
            return self.last

    client = Client_()
    publisher = PubSubPublisher(
        "demo-project", "relay-deliveries", timeout_seconds=7, client=client
    )
    assert (
        publisher.publish({"v": 1, "event_id": "e"}, {"event_id": "e", "correlation_id": "c"})
        == "msg-123"
    )
    assert published == [
        (
            "projects/demo-project/topics/relay-deliveries",
            b'{"v":1,"event_id":"e"}',
            {"event_id": "e", "correlation_id": "c"},
        )
    ]
    assert client.last is not None
    assert client.last.timeout == 7

    for failure in (TimeoutError("deadline"), RuntimeError("503 unavailable")):
        client.outcome = failure
        with pytest.raises(PublishError):
            publisher.publish({"v": 1}, {})


# --- internal authentication (cloud mode) -----------------------------------


def oidc_settings(**overrides: Any) -> Settings:
    values = {
        "internal_auth_mode": "oidc",
        "internal_oidc_audience": "https://worker.example",
        "internal_oidc_service_accounts": ["pubsub-invoker@p.iam.gserviceaccount.com"],
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_oidc_internal_auth_checks_audience_issuer_and_service_account() -> None:
    good = {
        "iss": "https://accounts.google.com",
        "email": "pubsub-invoker@p.iam.gserviceaccount.com",
        "email_verified": True,
    }
    seen: list[tuple[str, str]] = []

    def verifier(claims: dict[str, Any] | Exception) -> Any:
        def verify(token: str, audience: str) -> dict[str, Any]:
            seen.append((token, audience))
            if isinstance(claims, Exception):
                raise claims
            return claims

        return verify

    auth = InternalAuthenticator(oidc_settings(), oidc_verifier=verifier(good))
    assert (
        auth.authenticate({"authorization": "Bearer signed.jwt.token"})
        == "pubsub-invoker@p.iam.gserviceaccount.com"
    )
    assert seen == [("signed.jwt.token", "https://worker.example")]

    rejected: list[dict[str, Any] | Exception] = [
        ValueError("Token expired"),
        ValueError("Wrong audience"),
        {**good, "iss": "https://evil.example"},
        {**good, "email": "someone@gmail.com"},
        {**good, "email_verified": False},
        {**good, "email": ""},
    ]
    for claims in rejected:
        with pytest.raises(UnauthenticatedError):
            InternalAuthenticator(oidc_settings(), oidc_verifier=verifier(claims)).authenticate(
                {"authorization": "Bearer t"}
            )
    for headers in (
        {},
        {"authorization": "Basic abc"},
        {"authorization": "Bearer "},
        {"x-relay-internal-token": "anything"},
    ):
        with pytest.raises(UnauthenticatedError):
            auth.authenticate(headers)
    # An empty allowlist must not mean "any Google identity".
    open_door = InternalAuthenticator(
        oidc_settings(internal_oidc_service_accounts=[]), oidc_verifier=verifier(good)
    )
    with pytest.raises(UnauthenticatedError):
        open_door.authenticate({"authorization": "Bearer t"})


def test_token_internal_auth_fails_closed_when_unconfigured() -> None:
    unset = InternalAuthenticator(Settings(_env_file=None))  # type: ignore[call-arg]
    for headers in ({}, {"x-relay-internal-token": ""}, {"x-relay-internal-token": "guess"}):
        with pytest.raises(UnauthenticatedError):
            unset.authenticate(headers)


# --- model provider wire formats --------------------------------------------


def test_ollama_wire_format() -> None:
    bodies: list[dict[str, Any]] = []
    replies = [
        {
            "model": "llama3.1",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "get_event", "arguments": {"event_id": "e1"}}}
                ],
            },
            "prompt_eval_count": 50,
            "eval_count": 9,
        },
        {
            "model": "llama3.1",
            "message": {"role": "assistant", "content": "done"},
            "prompt_eval_count": 80,
            "eval_count": 4,
            "done_reason": "stop",
        },
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/chat"
        assert "authorization" not in req.headers  # local: no credential
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=replies[len(bodies) - 1])

    session = OllamaProvider(
        "http://127.0.0.1:11434/", "llama3.1", transport=httpx.MockTransport(handler)
    ).start(system="SYS", user="USER", tools=TOOLS, max_output_tokens=512)
    first = session.send(timeout=5)
    assert [(c.name, c.arguments) for c in first.tool_calls] == [("get_event", {"event_id": "e1"})]
    assert (first.usage.input_tokens, first.usage.output_tokens) == (50, 9)
    second = session.send(
        tool_results=[ToolResult(first.tool_calls[0].id, "get_event", '{"status":"DELIVERED"}')],
        timeout=5,
    )
    assert (second.text, second.tool_calls) == ("done", [])

    assert bodies[0]["stream"] is False
    assert bodies[0]["options"] == {"temperature": 0, "num_predict": 512}
    assert bodies[0]["tools"][0] == {
        "type": "function",
        "function": {
            "name": "get_event",
            "description": "Read an event.",
            "parameters": TOOLS[0].input_schema,
        },
    }
    assert [m["role"] for m in bodies[1]["messages"]] == ["system", "user", "assistant", "tool"]
    assert bodies[1]["messages"][3] == {
        "role": "tool",
        "tool_name": "get_event",
        "content": '{"status":"DELIVERED"}',
    }


def test_openai_compatible_wire_format() -> None:
    bodies: list[dict[str, Any]] = []
    replies = [
        {
            "model": "m-1",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_9",
                                "type": "function",
                                "function": {
                                    "name": "get_event",
                                    "arguments": '{"event_id": "e1"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 70, "completion_tokens": 11},
        },
        {
            "model": "m-1",
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": "final"}}
            ],
            "usage": {"prompt_tokens": 90, "completion_tokens": 3},
        },
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer sk-test"
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=replies[len(bodies) - 1])

    session = OpenAICompatProvider(
        "https://llm.example/v1", "sk-test", "m-1", transport=httpx.MockTransport(handler)
    ).start(system="SYS", user="USER", tools=TOOLS, max_output_tokens=256)
    first = session.send(timeout=5)
    assert (first.tool_calls[0].id, first.tool_calls[0].arguments) == (
        "call_9",
        {"event_id": "e1"},
    )  # arguments arrive as a JSON string
    second = session.send(
        tool_results=[ToolResult("call_9", "get_event", "{}")], user_text="finish up", timeout=5
    )
    assert second.text == "final"
    assert (bodies[0]["tool_choice"], bodies[0]["max_tokens"]) == ("auto", 256)
    assert bodies[1]["messages"][3] == {"role": "tool", "tool_call_id": "call_9", "content": "{}"}
    assert bodies[1]["messages"][4] == {"role": "user", "content": "finish up"}


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(401, json={}), "auth"),
        (httpx.Response(403, json={}), "auth"),
        (httpx.Response(429, json={}), "rate_limited"),
        (httpx.Response(500, json={}), "unavailable"),
        (httpx.Response(400, json={}), "bad_response"),
        (httpx.Response(200, content=b"<html>"), "bad_response"),
        (httpx.Response(200, json=[]), "bad_response"),
        (httpx.Response(200, json={"choices": []}), "bad_response"),
        (
            httpx.Response(
                200, json={"choices": [{"finish_reason": "length", "message": {"content": "cut"}}]}
            ),
            "truncated",
        ),
        (
            httpx.Response(
                200,
                json={"choices": [{"finish_reason": "content_filter", "message": {"content": ""}}]},
            ),
            "refused",
        ),
        (
            httpx.Response(
                200, json={"choices": [{"message": {"tool_calls": [{"function": {}}]}}]}
            ),
            "bad_response",
        ),
    ],
)
def test_http_provider_errors_map_to_fallback_kinds(response: httpx.Response, kind: str) -> None:
    session = OpenAICompatProvider(
        "https://llm.example/v1", "k", "m", transport=httpx.MockTransport(lambda r: response)
    ).start(system="s", user="u", tools=TOOLS, max_output_tokens=64)
    with pytest.raises(ProviderError) as info:
        session.send(timeout=1)
    assert info.value.kind == kind


def test_http_provider_transport_failures_map_to_fallback_kinds() -> None:
    def boom(exc: Exception) -> Any:
        def handler(req: httpx.Request) -> httpx.Response:
            raise exc

        return handler

    for exc, kind in (
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("refused"), "unavailable"),
    ):
        session = OllamaProvider(
            "http://127.0.0.1:11434", "m", transport=httpx.MockTransport(boom(exc))
        ).start(system="s", user="u", tools=TOOLS, max_output_tokens=64)
        with pytest.raises(ProviderError) as info:
            session.send(timeout=1)
        assert info.value.kind == kind


class FakeAnthropicClient:
    """Stands in for ``anthropic.Anthropic`` and records each request's keyword arguments."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.timeouts: list[float] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, *, timeout: float) -> FakeAnthropicClient:
        self.timeouts.append(timeout)
        return self

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def anthropic_response(stop_reason: str, content: list[Any], model: str = "claude-opus-5-5") -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason, content=content, model=model,
        usage=SimpleNamespace(input_tokens=100, output_tokens=30, cache_creation_input_tokens=5, cache_read_input_tokens=7),
    )  # fmt: skip


def test_anthropic_request_shape_and_append_only_history() -> None:
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig-opaque")
    tool_use = SimpleNamespace(
        type="tool_use", id="toolu_1", name="get_event", input={"event_id": "e1"}
    )
    final = SimpleNamespace(type="text", text="Done.")
    client = FakeAnthropicClient(
        [
            anthropic_response("tool_use", [thinking, tool_use]),
            anthropic_response("end_turn", [final]),
        ]
    )
    provider = AnthropicProvider("unused", "claude-opus-5-5", effort="medium", client=client)
    session = provider.start(system="SYS", user="USER", tools=TOOLS, max_output_tokens=16000)

    first = session.send(timeout=20)
    assert [(c.id, c.name, c.arguments) for c in first.tool_calls] == [
        ("toolu_1", "get_event", {"event_id": "e1"})
    ]
    assert (first.usage.input_tokens, first.usage.output_tokens) == (
        112,
        30,
    )  # includes cache tokens
    second = session.send(
        tool_results=[
            ToolResult("toolu_1", "get_event", '{"ok":1}'),
            ToolResult("toolu_2", "x", "err", True),
        ],
        user_text="wrap up",
        timeout=9,
    )
    assert (second.text, second.tool_calls) == ("Done.", [])
    assert client.timeouts == [20, 9]

    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["max_tokens"] == 16000
    assert req["system"] == "SYS"
    assert req["tools"] == [
        {
            "name": "get_event",
            "description": "Read an event.",
            "input_schema": TOOLS[0].input_schema,
        }
    ]
    assert req["output_config"] == {"effort": "medium"}
    # Refusal fallback is opted into, with the header that matches the "default" form.
    assert (req["fallbacks"], req["betas"]) == ("default", [FALLBACK_BETA])
    assert FALLBACK_BETA == "server-side-fallback-2026-07-01"
    # Parameters that current models reject must not be sent.
    for rejected in ("temperature", "top_p", "top_k", "tool_choice", "thinking"):
        assert rejected not in req

    history = client.requests[1]["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "user"]
    # The assistant turn is echoed back as the very same objects, thinking block included.
    assert history[1]["content"][0] is thinking
    assert history[1]["content"][1] is tool_use
    # All tool results travel in one user message, followed by any extra instruction.
    assert history[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"ok":1}', "is_error": False},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "err", "is_error": True},
        {"type": "text", "text": "wrap up"},
    ]


def test_anthropic_stop_reasons_and_errors_trigger_fallback() -> None:
    import anthropic

    def run(response: Any) -> ProviderError:
        session = AnthropicProvider(
            "k", "claude-opus-5-5", client=FakeAnthropicClient([response])
        ).start(system="s", user="u", tools=TOOLS, max_output_tokens=1024)
        with pytest.raises(ProviderError) as info:
            session.send(timeout=1)
        return info.value

    # A refusal is HTTP 200 with possibly empty content: stop_reason is checked first.
    assert run(anthropic_response("refusal", [])).kind == "refused"
    assert (
        run(anthropic_response("max_tokens", [SimpleNamespace(type="text", text="cut off")])).kind
        == "truncated"
    )

    http = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def status_error(cls: Any, status: int) -> Exception:
        return cls("boom", response=httpx.Response(status, request=http), body=None)

    try:
        cases = [
            (anthropic.APITimeoutError(request=http), "timeout"),
            (anthropic.APIConnectionError(request=http), "unavailable"),
            (status_error(anthropic.RateLimitError, 429), "rate_limited"),
            (status_error(anthropic.AuthenticationError, 401), "auth"),
            (status_error(anthropic.PermissionDeniedError, 403), "auth"),
            (status_error(anthropic.InternalServerError, 500), "unavailable"),
            (status_error(anthropic.BadRequestError, 400), "bad_response"),
        ]
    except TypeError as exc:  # pragma: no cover - SDK constructor drift
        pytest.skip(f"anthropic exception constructors changed: {exc}")
    for exc, kind in cases:
        assert run(exc).kind == kind, type(exc).__name__


# --- MCP server -------------------------------------------------------------


def test_mcp_server_exposes_only_read_only_workspace_scoped_tools(harness: Harness) -> None:
    ours = harness.seeded
    assert ours is not None
    harness.destination.enqueue(400, {"error": "nope"})
    event_id = harness.accepted_event_id(harness.send_webhook(order(), event_id="mcp-1"))
    harness.pump()
    theirs = harness.seed(slug="rival")

    async def exercise() -> dict[str, Any]:
        out: dict[str, Any] = {}
        async with Client(build_server(harness.session_factory, ours.workspace_id)) as client:
            listing = await client.list_tools()
            out["tools"] = {t.name: t for t in listing.tools}
            out["event"] = await client.call_tool("get_event", {"event_id": event_id})
            out["attempts"] = await client.call_tool(
                "list_delivery_attempts", {"event_id": event_id}
            )
            out["bad_args"] = await client.call_tool(
                "get_event", {"event_id": event_id, "workspace_id": str(theirs.workspace_id)}
            )
            out["dry_run"] = await client.call_tool(
                "validate_proposed_mapping",
                {
                    "event_id": event_id,
                    "mappings": [
                        {"target": "order_id", "source": "order_no"},
                        {"target": "quantity", "const": 3},
                    ],
                },
            )
        async with Client(build_server(harness.session_factory, theirs.workspace_id)) as other:
            out["cross_workspace"] = await other.call_tool("get_event", {"event_id": event_id})
        return out

    out = asyncio.run(exercise())
    tools = out["tools"]
    assert set(tools) == {t.name for t in READ_ONLY_TOOLS}
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.open_world_hint is False
        assert tool.input_schema["additionalProperties"] is False

    assert out["event"].is_error is False
    event_view = out["event"].structured_content
    assert event_view["status"] == "DEAD_LETTERED"
    assert event_view["payload_untrusted"]["payment"]["card_number"] == "[REDACTED]"
    assert json.loads(out["event"].content[0].text) == event_view
    assert out["attempts"].structured_content["attempts"][0]["http_status"] == 400

    # The workspace is fixed when the server starts; a caller cannot name another.
    assert out["bad_args"].is_error is True
    assert out["cross_workspace"].is_error is True
    assert out["cross_workspace"].structured_content == {
        "error": "event not found in this workspace"
    }

    assert out["dry_run"].structured_content["valid"] is True
    assert "Nothing was saved" in out["dry_run"].structured_content["note"]
    assert (
        harness.login(ours.admin)
        .get(f"/api/v1/integrations/{ours.integration_id}")
        .json()["current_version"]
        == 1
    )
    assert harness.event(event_id).status == "DEAD_LETTERED"
