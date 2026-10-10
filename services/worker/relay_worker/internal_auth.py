"""Authentication for the worker's internal endpoints.

The worker is never called by browsers or source systems — only by the message
broker's push subscription and the dispatcher scheduler.

* ``oidc`` (cloud): Pub/Sub and Cloud Scheduler attach a Google-signed OIDC
  identity token. It is verified here (signature, audience, issuer, expiry) and
  the caller's service account must be on an allowlist. Cloud Run IAM enforces
  the same thing at the platform edge; this check is defence in depth.
* ``token`` (local/test): a shared secret in ``X-Relay-Internal-Token``,
  compared in constant time. With no token configured, every call is refused.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable, Mapping
from typing import Any

from relay_core.config import Settings
from relay_core.errors import UnauthenticatedError

logger = logging.getLogger(__name__)

TOKEN_HEADER = "x-relay-internal-token"  # noqa: S105 - a header name
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})

# (token, audience) -> verified claims. Raises on any verification failure.
OidcVerifier = Callable[[str, str], Mapping[str, Any]]


def google_oidc_verifier(token: str, audience: str) -> Mapping[str, Any]:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    claims: Mapping[str, Any] = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
        token, google_requests.Request(), audience=audience
    )
    return claims


class InternalAuthenticator:
    def __init__(
        self, settings: Settings, *, oidc_verifier: OidcVerifier = google_oidc_verifier
    ) -> None:
        self._mode = settings.internal_auth_mode
        self._token = (
            settings.internal_token.get_secret_value() if settings.internal_token else None
        )
        self._audience = settings.internal_oidc_audience
        self._accounts = frozenset(a.lower() for a in settings.internal_oidc_service_accounts)
        self._verify = oidc_verifier
        if self._mode == "token" and not self._token:
            logger.error("RELAY_INTERNAL_TOKEN is not set: every internal request will be rejected")

    def authenticate(self, headers: Mapping[str, str]) -> str:
        """Return a caller identity for logging, or raise ``UnauthenticatedError``."""
        if self._mode == "token":
            provided = headers.get(TOKEN_HEADER, "")
            if not self._token or not hmac.compare_digest(provided.encode(), self._token.encode()):
                raise UnauthenticatedError("internal authentication failed")
            return "shared-token"

        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or self._audience is None:
            raise UnauthenticatedError("internal authentication failed")
        try:
            claims = self._verify(token.strip(), self._audience)
        except Exception as exc:
            logger.warning("OIDC token rejected", extra={"error": type(exc).__name__})
            raise UnauthenticatedError("internal authentication failed") from exc
        email = str(claims.get("email", "")).lower()
        if claims.get("iss") not in GOOGLE_ISSUERS or claims.get("email_verified") is not True:
            raise UnauthenticatedError("internal authentication failed")
        # An empty allowlist must not mean "anyone with a Google account".
        if not self._accounts or email not in self._accounts:
            logger.warning("OIDC caller is not an allowed service account")
            raise UnauthenticatedError("internal authentication failed")
        return email
