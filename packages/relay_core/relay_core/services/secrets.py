"""Secret references.

The database stores *references* such as ``env:RELAY_SECRET_ORDERS_WEBHOOK``,
never secret values. At runtime a reference resolves to an environment
variable; in Cloud Run those variables are populated from Secret Manager, so
rotation is "add a secret version, deploy a new revision".

References are restricted to the ``RELAY_SECRET_`` namespace. Without that
restriction an administrator could point a destination's auth header at
``RELAY_DATABASE_URL`` and have Relay post its own credentials to their server.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Final, Protocol

SECRET_REF_PATTERN: Final = re.compile(r"^env:(RELAY_SECRET_[A-Z0-9_]{1,64})$")


def validate_secret_ref(ref: str) -> str | None:
    """Return a reason the reference is unacceptable, or ``None`` if it is well-formed."""
    if not SECRET_REF_PATTERN.fullmatch(ref):
        return (
            "secret reference must look like 'env:RELAY_SECRET_<NAME>' "
            "(upper-case letters, digits and underscores)"
        )
    return None


class SecretResolver(Protocol):
    def resolve(self, ref: str) -> str | None:
        """Return the secret value, or ``None`` if it is not available."""
        ...


class EnvSecretResolver:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def resolve(self, ref: str) -> str | None:
        match = SECRET_REF_PATTERN.fullmatch(ref)
        if match is None:
            return None
        return self._environ.get(match.group(1)) or None
