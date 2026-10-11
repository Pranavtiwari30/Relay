# Security rules

These apply everywhere. Details and the threat model: `docs/architecture/security.md`.

## Secrets

- Never commit, log, print or send to a model: `.env` contents, API keys, session secrets, cloud
  credentials, database URLs with passwords. Do not `cat .env` or echo secret variables.
- The database stores secret **references** (`env:RELAY_SECRET_<NAME>`), never values. Do not widen
  the reference pattern beyond the `RELAY_SECRET_` namespace.
- No long-lived cloud keys. CI uses OIDC / Workload Identity Federation.
- A secret-scan finding is investigated, not silenced. Confirm the value is not real (if it is,
  rotate it; deleting it from Git is not enough). Prefer fixing the fixture. `.gitleaksignore` is
  only for reviewed findings already in history, one exact fingerprint per line, each with a
  written reason. Never allowlist a directory or a whole rule.

## Authentication and authorisation

- Every control-plane route depends on `require(Permission.X)`. A new route without it is a bug.
- Enforce on the server. Hiding a control in the UI is not access control.
- Every tenant query filters on `workspace_id`; cross-tenant ids return 404.
- No home-made cryptography. Passwords: Argon2id. Tokens: `secrets`. Comparisons of secrets:
  `hmac.compare_digest`.
- State-changing requests require the CSRF token and an allowlisted Origin. CORS is an explicit
  allowlist; never `*`.

## Inbound

- Verify webhook signatures over the **raw** body before parsing. One generic error for all
  signature failures.
- Enforce body-size limits before buffering. Reject non-object JSON, NaN/Infinity and NUL.

## Outbound (SSRF)

- Requests to operator-supplied URLs go only through `HttpDestinationAdapter`: resolve, require all
  addresses public, connect to the pinned address, no redirects, no proxy env, bounded response.
- Do not add a code path that fetches an arbitrary URL, and do not relax `is_public_address`.
- `RELAY_DESTINATION_ALLOW_PRIVATE_HOSTS` is a development override; the cloud profile must keep
  rejecting it.

## Data exposure

- No endpoint returns a raw payload. Redact on every read path, before storing response summaries,
  and before model context.
- Error responses never include internals or echo rejected input.

## Never

- `eval`, `exec`, `pickle` on untrusted data, shell commands built from input, or SQL built by
  string concatenation.
- JSON Schema `$ref` resolution or regular expressions from user-supplied configuration.

## Investigation assistant and MCP

- The tool registry is read-only. Do not add a tool that writes, replays, fetches URLs, runs SQL or
  shell, or reads files.
- Tools run in a `READ ONLY` transaction and are workspace-scoped (event-scoped during an
  investigation).
- Treat model output as untrusted: validate structure, verify evidence references against what
  tools returned, force `requires_approval`.
- Recording approval of a recommendation must never execute it.
- Keep steps, tool calls, result size and time bounded.
