"""SSRF URL validation, webhook signatures, schema restrictions and redaction."""

from __future__ import annotations

import copy
import ipaddress
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from relay_core.domain.redaction import (
    REDACTED,
    is_sensitive_key,
    redact,
    redact_text,
    summarise_text,
)
from relay_core.domain.schema import check_schema_definition, validate_instance
from relay_core.domain.signatures import (
    SignatureError,
    compute_signature,
    format_signature,
    verify_signature,
)
from relay_core.domain.urlsafety import (
    DestinationBlockedError,
    DestinationResolutionError,
    UrlPolicy,
    check_url_static,
    is_public_address,
    parse_destination,
    resolve_destination,
)

pytestmark = pytest.mark.security

STRICT = UrlPolicy()


def resolver_returning(*addresses: str):  # type: ignore[no-untyped-def]
    return lambda host, port: list(addresses)


# --- SSRF: address classification ------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.255.255.254",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "192.0.2.10",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd12:3456::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "2002:7f00:0001::1",  # 6to4 wrapping 127.0.0.1
        "2002:a9fe:a9fe::1",  # 6to4 wrapping 169.254.169.254
        "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
        "2001:0000:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
        "2001:db8::1",  # documentation
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    assert not is_public_address(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_accepted(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address))


# --- SSRF: static URL checks -----------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/orders",  # plain http
        "ftp://api.example.com/x",
        "file:///etc/passwd",
        "gopher://api.example.com/",
        "https://user:pass@api.example.com/",
        "https://api.example.com/#frag",
        "https://localhost/x",
        "https://LOCALHOST./x",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://printer.local/",
        "https://intranet/",  # not fully qualified
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.1.2.3:8443/",
        "https://[::ffff:10.0.0.1]/",
        "https://api.example.com:0/",
        "https://api.example.com:99999/",
        "https:///nohost",
        "https://exa mple.com/",
        "https://example.com/a\nb",
        "https://example.com\\@127.0.0.1/",
        "https://exämple.com/",
        "",
        "https://example.com/" + "a" * 2100,
    ],
)
def test_static_check_blocks_unsafe_urls(url: str) -> None:
    assert check_url_static(url, STRICT) is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/orders",
        "https://api.example.com:8443/v1/orders?tenant=7",
        "https://8.8.8.8/hook",
    ],
)
def test_static_check_accepts_public_https_urls(url: str) -> None:
    assert check_url_static(url, STRICT) is None


def test_display_url_drops_query_string() -> None:
    parsed = parse_destination("https://api.example.com:8443/v1/orders?api_key=abc", STRICT)
    assert parsed.display == "https://api.example.com:8443/v1/orders"
    assert parsed.host_header == "api.example.com:8443"


# --- SSRF: resolution-time checks and pinning ------------------------------


def test_resolution_pins_the_validated_address() -> None:
    resolved = resolve_destination(
        "https://api.example.com/orders?x=1", STRICT, resolver_returning("93.184.216.34")
    )
    assert resolved.address == "93.184.216.34"
    assert resolved.connect_url == "https://93.184.216.34:443/orders?x=1"
    assert resolved.parsed.host_header == "api.example.com"


def test_ipv6_connect_url_is_bracketed() -> None:
    resolved = resolve_destination(
        "https://api.example.com/", STRICT, resolver_returning("2606:4700:4700::1111")
    )
    assert resolved.connect_url == "https://[2606:4700:4700::1111]:443/"


@pytest.mark.parametrize(
    "private", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "::ffff:192.168.0.1"]
)
def test_public_name_resolving_to_private_address_is_blocked(private: str) -> None:
    with pytest.raises(DestinationBlockedError, match="non-public"):
        resolve_destination("https://rebind.example.com/", STRICT, resolver_returning(private))


def test_one_private_record_among_public_ones_blocks_the_request() -> None:
    """DNS rebinding: never 'pick the good record' from a mixed answer."""
    with pytest.raises(DestinationBlockedError):
        resolve_destination(
            "https://rebind.example.com/", STRICT, resolver_returning("93.184.216.34", "10.0.0.5")
        )


def test_numeric_host_aliases_are_caught_at_resolution() -> None:
    # "2130706433" is not an IP literal to the parser, but resolvers map it to 127.0.0.1.
    assert check_url_static("https://2130706433/", STRICT) is not None
    with pytest.raises(DestinationBlockedError):
        resolve_destination(
            "https://017700000001.example.com/", STRICT, resolver_returning("127.0.0.1")
        )


def test_dns_failure_is_transient_not_blocked() -> None:
    def failing(host: str, port: int) -> list[str]:
        raise OSError("temporary failure in name resolution")

    with pytest.raises(DestinationResolutionError):
        resolve_destination("https://api.example.com/", STRICT, failing)
    with pytest.raises(DestinationResolutionError):
        resolve_destination("https://api.example.com/", STRICT, resolver_returning())


def test_private_targets_need_an_explicit_exact_allowlist_entry() -> None:
    dev = UrlPolicy(allow_private_hosts=frozenset({"localhost:9000"}))
    resolved = resolve_destination(
        "http://localhost:9000/hook", dev, resolver_returning("127.0.0.1")
    )
    assert resolved.connect_url == "http://127.0.0.1:9000/hook"
    # A different port or host on the same machine is still blocked.
    assert check_url_static("http://localhost:9001/hook", dev) is not None
    assert check_url_static("http://127.0.0.1:9000/hook", dev) is not None
    # The allowlist does not turn on plain http for public hosts.
    assert check_url_static("http://api.example.com/", dev) is not None


# --- webhook signatures ----------------------------------------------------

SECRET = b"test-signing-secret"
BODY = b'{"id":"ord_1","qty":2}'
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
TS = str(int(NOW.timestamp()))


def verify(**overrides: Any) -> None:
    args: dict[str, Any] = {
        "secret": SECRET,
        "body": BODY,
        "signature_header": format_signature(compute_signature(SECRET, BODY, TS)),
        "timestamp_header": TS,
        "require_timestamp": True,
        "now": NOW,
        "tolerance_seconds": 300,
    }
    args.update(overrides)
    verify_signature(**args)


def test_valid_signature_is_accepted() -> None:
    verify()
    verify(signature_header=compute_signature(SECRET, BODY, TS).upper())  # bare hex, any case


def test_body_only_signature_mode() -> None:
    verify(
        signature_header=format_signature(compute_signature(SECRET, BODY)),
        timestamp_header=None,
        require_timestamp=False,
    )


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"signature_header": None}, "signature_missing"),
        ({"signature_header": ""}, "signature_missing"),
        ({"signature_header": "sha256=nothex"}, "signature_malformed"),
        ({"signature_header": "sha256=" + "a" * 63}, "signature_malformed"),
        ({"signature_header": "sha256=" + "0" * 64}, "signature_mismatch"),
        ({"body": BODY + b" "}, "signature_mismatch"),
        ({"secret": b"other"}, "signature_mismatch"),
        ({"timestamp_header": None}, "timestamp_missing"),
        ({"timestamp_header": "yesterday"}, "timestamp_malformed"),
        ({"timestamp_header": str(int(NOW.timestamp()) + 1)}, "signature_mismatch"),
    ],
)
def test_invalid_signatures_are_rejected(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(SignatureError) as info:
        verify(**overrides)
    assert info.value.code == code


@pytest.mark.parametrize("skew", [timedelta(minutes=6), timedelta(minutes=-6)])
def test_correctly_signed_but_stale_request_is_rejected(skew: timedelta) -> None:
    """A captured request cannot be replayed outside the tolerance window."""
    with pytest.raises(SignatureError) as info:
        verify(now=NOW + skew)
    assert info.value.code == "timestamp_out_of_tolerance"


def test_signature_for_one_timestamp_does_not_validate_another() -> None:
    later = str(int(TS) + 60)
    with pytest.raises(SignatureError):
        verify(timestamp_header=later)


# --- JSON Schema subset ----------------------------------------------------


@pytest.mark.parametrize(
    "keyword_schema",
    [
        {"$ref": "https://169.254.169.254/schema.json"},
        {"$ref": "#/$defs/x", "$defs": {"x": {}}},
        {"$id": "https://example.com/s"},
        {"properties": {"a": {"pattern": "^(a+)+$"}}},
        {"patternProperties": {"^x": {}}},
        {"items": {"$dynamicRef": "#x"}},
        {"anyOf": [{"if": {}, "then": {}}]},
    ],
)
def test_schema_keywords_that_enable_ssrf_or_redos_are_rejected(
    keyword_schema: dict[str, Any],
) -> None:
    issues = check_schema_definition(keyword_schema, location="input_schema")
    assert issues
    assert all(issue.code == "unsupported_schema_keyword" for issue in issues)


def test_schema_structural_limits() -> None:
    deep: dict[str, Any] = {"type": "object"}
    for _ in range(20):
        deep = {"type": "object", "properties": {"n": deep}}
    assert check_schema_definition(deep, location="s")[0].code == "schema_too_deep"
    wide = {"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(2500)}}
    assert check_schema_definition(wide, location="s")[-1].code == "schema_too_large"
    assert check_schema_definition([1], location="s")[0].code == "invalid_schema"
    assert check_schema_definition({"type": "strng"}, location="s")[0].code == "invalid_schema"


def test_validation_messages_never_contain_payload_values() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "card": {"type": "integer"},
            "tier": {"enum": ["a", "b"]},
            "name": {"type": "string", "maxLength": 3},
        },
    }
    secret = "4111111111111111-SECRET-VALUE"
    issues = validate_instance(
        schema, {"card": secret, "tier": secret, "name": secret, "extra": secret}
    )
    assert len(issues) == 4
    assert all(secret not in issue.message and "SECRET" not in issue.message for issue in issues)
    assert any("extra" in issue.message for issue in issues)


def test_validation_reports_nested_locations_and_caps_errors() -> None:
    schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "object", "required": ["sku"]}}
        },
    }
    issues = validate_instance(schema, {"items": [{"sku": "a"}, {}, {"sku": "c"}, {}]})
    assert [issue.path for issue in issues] == ["items[1].sku", "items[3].sku"]
    many = validate_instance({"type": "array", "items": {"type": "string"}}, list(range(500)))
    assert len(many) == 25


# --- redaction -------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Password",
        "user_password",
        "api-key",
        "API_KEY",
        "x-api-key",
        "Authorization",
        "access_token",
        "refreshToken",
        "client_secret",
        "card_number",
        "cvv",
        "ssn",
        "Set-Cookie",
        "session_id",
        "X-Relay-Signature",
        "privateKey".replace("K", "_k"),
    ],
)
def test_sensitive_key_names_are_detected(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", ["name", "qty", "sku", "email", "order_id", "status", "total"])
def test_ordinary_keys_are_not_redacted(key: str) -> None:
    assert not is_sensitive_key(key)


def test_redact_walks_nested_structures_without_mutating_input() -> None:
    payload = {
        "id": "ord_1",
        "customer": {"email": "ada@example.com", "password": "hunter2", "loyalty_no": "L-9"},
        "payment": {
            "card_number": "4111 1111 1111 1111",
            "note": "card 4111-1111-1111-1111 on file",
        },
        "headers": [{"Authorization": "Bearer abcdefghijklmnop"}],
        "callback": "https://svc:s3cr3t@example.com/cb?token=abc123def",
    }
    before = copy.deepcopy(payload)
    out = redact(payload, extra_keys=["loyalty_no"])
    assert payload == before
    assert out["id"] == "ord_1"
    assert out["customer"] == {
        "email": "ada@example.com",
        "password": REDACTED,
        "loyalty_no": REDACTED,
    }
    assert out["payment"]["card_number"] == REDACTED
    assert "4111" not in out["payment"]["note"]
    assert out["headers"][0]["Authorization"] == REDACTED
    assert "s3cr3t" not in out["callback"]
    assert "abc123def" not in out["callback"]


def test_redact_text_masks_credentials_but_keeps_ordinary_numbers() -> None:
    text = 'Authorization: Bearer example-bearer-abcdefgh12345 failed; api_key="zzz999"; order 1234567890123456'
    masked = redact_text(text)
    assert "example-bearer-abcdefgh12345" not in masked
    assert "zzz999" not in masked
    assert "1234567890123456" in masked  # fails the Luhn check, so it is not a card number


def test_summarise_text_redacts_json_strips_control_chars_and_truncates() -> None:
    assert summarise_text('{"error":"bad","token":"abc"}') == '{"error":"bad","token":"[REDACTED]"}'
    assert "\x1b" not in summarise_text("oops\x1b[31m red")
    long = summarise_text("x" * 5000, max_chars=100)
    assert long.startswith("x" * 100)
    assert "truncated, 5000 chars" in long


_json = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=20)),
    lambda c: st.one_of(
        st.lists(c, max_size=4), st.dictionaries(st.text(max_size=10), c, max_size=4)
    ),
    max_leaves=25,
)


@given(value=_json)
def test_redact_is_idempotent_and_shape_preserving(value: Any) -> None:
    once = redact(value)
    assert redact(once) == once
    assert type(once) is type(value)
