# ADR-0006: Server-side sessions behind a same-origin proxy

- **Status:** Accepted
- **Date:** 2026-10-11

## Context

The UI is a static site and the API is a separate service, which by default puts them on different
origins. Browsers increasingly block third-party cookies, and tokens kept in JavaScript-readable
storage are exposed to any script injection.

## Decision

- **Opaque server-side sessions** in an `HttpOnly` cookie. Only a SHA-256 of the token is stored.
  Sessions expire and can be revoked; a password change revokes the others.
- **Same origin.** The static host proxies `/api` and `/webhooks` to the API (a Vite proxy in
  development, a Cloudflare Pages Function in production), so the cookie is first-party and
  `SameSite=Lax` suffices.
- **CSRF protection regardless:** a per-session token in `X-CSRF-Token` plus an `Origin` allowlist
  on every state-changing request.
- **Role read on every request** from the database, so a demotion takes effect immediately.

## Alternatives considered

- **JWT in `localStorage`.** No CSRF exposure, but readable by injected script and awkward to
  revoke.
- **Cross-site cookies (`SameSite=None`).** Broken by third-party cookie blocking in several
  browsers.
- **An external identity provider.** The right answer for a product; out of scope for a
  self-contained portfolio project, and it would add a hard external dependency to the local demo.

## Consequences

- Each authenticated request costs a session lookup (indexed).
- The Pages Function consumes Workers free-plan requests (100,000 per day shared).
- No MFA, SSO or self-service password reset — recorded as out of scope in `security.md`.
