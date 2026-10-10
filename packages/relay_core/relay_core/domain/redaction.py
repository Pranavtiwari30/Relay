"""Redaction of sensitive values before display, storage of summaries, or model context.

Redaction is defence in depth, not a guarantee: it matches well-known key names
and value shapes. Integrations can add their own key names (``redact_fields``).
Email addresses are intentionally left visible because operators need them to
identify an order; add ``email`` to ``redact_fields`` where that is not wanted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Final

REDACTED: Final = "[REDACTED]"

_SENSITIVE_PARTS: Final = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "credential",
    "card_number",
    "cardnumber",
    "cvv",
    "cvc",
    "ssn",
    "cookie",
    "session",
    "signature",
    "iban",
    "account_number",
)

_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,20}://)[^/\s:@]{1,256}:[^/\s@]{1,256}@")
_KV_RE = re.compile(
    r"(?i)\b((?:[a-z0-9_-]{0,32})(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)"
    r"(?:[a-z0-9_-]{0,32}))(\s{0,3}[=:]\s{0,3})(\"?)[^\s\"&,;}]{1,512}"
)
_DIGIT_RUN_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")


def _normalise(key: str) -> str:
    return key.lower().replace("-", "_").replace(" ", "_")


def is_sensitive_key(key: str, extra_keys: Iterable[str] = ()) -> bool:
    normalised = _normalise(key)
    if any(part in normalised for part in _SENSITIVE_PARTS):
        return True
    return any(normalised == _normalise(extra) for extra in extra_keys)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return REDACTED if 13 <= len(digits) <= 19 and _luhn_ok(digits) else match.group(0)


def redact_text(text: str) -> str:
    """Mask credential-shaped substrings in free text."""
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}{REDACTED}@", text)
    text = _KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}", text)
    return _DIGIT_RUN_RE.sub(_mask_card, text)


def redact(value: Any, *, extra_keys: Iterable[str] = (), _depth: int = 0) -> Any:
    """Return a redacted deep copy of a JSON value. The input is never mutated."""
    extras = tuple(extra_keys)
    if _depth > 32:
        return REDACTED
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and is_sensitive_key(key, extras)
                else redact(item, extra_keys=extras, _depth=_depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, extra_keys=extras, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def summarise_text(text: str, *, max_chars: int = 2000, extra_keys: Iterable[str] = ()) -> str:
    """Redact and truncate untrusted text (for example a destination response body)."""
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if parsed is not None:
            stripped = json.dumps(redact(parsed, extra_keys=extra_keys), separators=(",", ":"))
        else:
            stripped = redact_text(stripped)
    else:
        stripped = redact_text(stripped)
    stripped = "".join(ch if ch >= " " or ch in "\n\t" else " " for ch in stripped)
    if len(stripped) > max_chars:
        return stripped[:max_chars] + f"… [truncated, {len(stripped)} chars]"
    return stripped
