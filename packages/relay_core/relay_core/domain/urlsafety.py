"""SSRF defences for operator-configured destination URLs.

Two layers:

1. ``check_url_static`` — at configuration time: scheme, credentials-in-URL,
   literal private addresses, obvious local names.
2. ``resolve_destination`` — at every delivery attempt: resolve the hostname,
   require *every* resolved address to be public, and return one address for
   the HTTP client to connect to directly ("pinning"). Pinning closes the DNS
   rebinding gap between "check" and "connect".

Redirects are never followed by the HTTP adapter, so a public host cannot
bounce a request to an internal one. Remaining limits are documented in
``docs/architecture/security.md``.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import SplitResult, urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], list[str]]

_HOSTNAME_RE: Final = re.compile(
    r"^(?=.{1,253}$)([a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*$"
)
_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")
_LOCAL_SUFFIXES: Final = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
MAX_URL_LENGTH: Final = 2048


class DestinationBlockedError(Exception):
    """The URL is not an acceptable delivery target. ``reason`` is safe to show operators."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DestinationResolutionError(Exception):
    """DNS failed. Unlike a blocked URL this is transient and may be retried."""


@dataclass(frozen=True)
class UrlPolicy:
    allow_http: bool = False
    # Exact "host" or "host:port" entries that may resolve to non-public
    # addresses. Development and the built-in demo destination only.
    allow_private_hosts: frozenset[str] = frozenset()

    def permits_private(self, host: str, port: int) -> bool:
        return host in self.allow_private_hosts or f"{host}:{port}" in self.allow_private_hosts


@dataclass(frozen=True)
class ParsedDestination:
    scheme: str
    host: str
    port: int
    target: str  # path + query, always starting with "/"
    literal_ip: IPAddress | None

    @property
    def default_port(self) -> bool:
        return self.port == (443 if self.scheme == "https" else 80)

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if isinstance(self.literal_ip, ipaddress.IPv6Address) else self.host
        return host if self.default_port else f"{host}:{self.port}"

    @property
    def display(self) -> str:
        """URL safe for logs and the UI: no query string, no credentials."""
        return f"{self.scheme}://{self.host_header}{self.target.split('?', 1)[0]}"


@dataclass(frozen=True)
class ResolvedDestination:
    parsed: ParsedDestination
    address: str  # the single address the client must connect to

    @property
    def connect_url(self) -> str:
        host = f"[{self.address}]" if ":" in self.address else self.address
        return f"{self.parsed.scheme}://{host}:{self.parsed.port}{self.parsed.target}"


def is_public_address(address: IPAddress) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(address, ipaddress.IPv6Address):
        # Forms that smuggle an IPv4 address past an IPv6-only check.
        if address.ipv4_mapped is not None:
            return is_public_address(address.ipv4_mapped)
        if address.sixtofour is not None:
            return is_public_address(address.sixtofour)
        if address.teredo is not None:
            return False
        if address in _NAT64:
            return is_public_address(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_unspecified
    )


def _split(url: str) -> SplitResult:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise DestinationBlockedError(f"URL must be 1 to {MAX_URL_LENGTH} characters")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url) or "\\" in url:
        raise DestinationBlockedError("URL contains whitespace, control characters or backslashes")
    try:
        return urlsplit(url)
    except ValueError as exc:
        raise DestinationBlockedError("URL is not well-formed") from exc


def parse_destination(url: str, policy: UrlPolicy) -> ParsedDestination:
    parts = _split(url)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise DestinationBlockedError("only https:// destinations are supported")
    if parts.username is not None or parts.password is not None:
        raise DestinationBlockedError("credentials must not be embedded in the URL")
    if parts.fragment:
        raise DestinationBlockedError("URL fragments are not allowed")
    try:
        host = (parts.hostname or "").lower().rstrip(".")
        explicit_port = parts.port
        port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise DestinationBlockedError("URL has an invalid port") from exc
    if not host:
        raise DestinationBlockedError("URL has no host")
    if not 1 <= port <= 65535:
        raise DestinationBlockedError("URL has an invalid port")

    literal: IPAddress | None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
        if not host.isascii() or not _HOSTNAME_RE.fullmatch(host):
            raise DestinationBlockedError("URL host is not a valid hostname") from None

    private_ok = policy.permits_private(host, port)
    if scheme == "http" and not (policy.allow_http or private_ok):
        raise DestinationBlockedError("plain http:// destinations are not allowed; use https://")
    if not private_ok:
        if literal is not None and not is_public_address(literal):
            raise DestinationBlockedError("destination address is not publicly routable")
        if literal is None and (host == "localhost" or host.endswith(_LOCAL_SUFFIXES)):
            raise DestinationBlockedError("destination host is a local or internal name")
        if literal is None and "." not in host:
            raise DestinationBlockedError("destination host must be a fully qualified name")

    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    return ParsedDestination(scheme, host, port, target, literal)


def check_url_static(url: str, policy: UrlPolicy) -> str | None:
    """Configuration-time check. Returns a reason string, or ``None`` if acceptable."""
    try:
        parse_destination(url, policy)
    except DestinationBlockedError as exc:
        return exc.reason
    return None


def system_resolver(host: str, port: int) -> list[str]:
    infos: Iterable[Any] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        if address not in seen:
            seen.append(address)
    return seen


def resolve_destination(
    url: str, policy: UrlPolicy, resolver: Resolver = system_resolver
) -> ResolvedDestination:
    """Resolve and validate ``url`` for one delivery attempt."""
    parsed = parse_destination(url, policy)
    private_ok = policy.permits_private(parsed.host, parsed.port)
    if parsed.literal_ip is not None:
        return ResolvedDestination(parsed, str(parsed.literal_ip))
    try:
        addresses = resolver(parsed.host, parsed.port)
    except OSError as exc:
        raise DestinationResolutionError(f"could not resolve host '{parsed.host}'") from exc
    if not addresses:
        raise DestinationResolutionError(f"host '{parsed.host}' has no addresses")
    if not private_ok:
        for raw in addresses:
            try:
                candidate = ipaddress.ip_address(raw)
            except ValueError as exc:
                raise DestinationBlockedError("resolver returned an invalid address") from exc
            # One private record is enough to block: never pick "the good one".
            if not is_public_address(candidate):
                raise DestinationBlockedError("destination host resolves to a non-public address")
    return ResolvedDestination(parsed, addresses[0])
