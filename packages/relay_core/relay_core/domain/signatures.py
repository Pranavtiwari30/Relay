"""Webhook HMAC-SHA256 signatures.

The signed message is ``"<timestamp>." + raw_body`` when a timestamp is used
(recommended: it bounds replay of a captured request), otherwise the raw body.
Verification always uses a constant-time comparison.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime


class SignatureError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def compute_signature(secret: bytes, body: bytes, timestamp: str | None = None) -> str:
    message = f"{timestamp}.".encode() + body if timestamp is not None else body
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def format_signature(hex_digest: str) -> str:
    return f"sha256={hex_digest}"


def verify_signature(
    *,
    secret: bytes,
    body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
    require_timestamp: bool,
    now: datetime,
    tolerance_seconds: int,
) -> None:
    """Raise ``SignatureError`` unless the request is authentic and fresh."""
    if not signature_header:
        raise SignatureError("signature_missing", "signature header is missing")
    provided = signature_header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[7:]
    provided = provided.strip().lower()
    if len(provided) != 64 or any(ch not in "0123456789abcdef" for ch in provided):
        raise SignatureError("signature_malformed", "signature must be a SHA-256 hex digest")

    timestamp: str | None = None
    if require_timestamp:
        if not timestamp_header:
            raise SignatureError("timestamp_missing", "timestamp header is missing")
        timestamp = timestamp_header.strip()
        if not timestamp.isascii() or not timestamp.isdigit() or len(timestamp) > 12:
            raise SignatureError("timestamp_malformed", "timestamp must be Unix seconds")

    expected = compute_signature(secret, body, timestamp)
    # Compare before checking freshness so a stale-but-forged request learns nothing.
    if not hmac.compare_digest(expected, provided):
        raise SignatureError("signature_mismatch", "signature does not match")
    if timestamp is not None and abs(now.timestamp() - int(timestamp)) > tolerance_seconds:
        raise SignatureError(
            "timestamp_out_of_tolerance", "timestamp is outside the allowed window"
        )
