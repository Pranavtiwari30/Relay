# API contracts

The authoritative, machine-readable contract is the OpenAPI document: served at `/openapi.json`
(interactive at `/docs`) and exported to [openapi.json](openapi.json) with `make openapi`. This
page describes the conventions that document follows and the endpoints worth knowing.

## Conventions

| Topic | Rule |
|---|---|
| **Base path** | Control plane under `/api/v1`. Webhooks at `/webhooks/{connection_id}`. Health at `/healthz`, `/readyz`. Worker callbacks under `/internal/…` on the worker service only. |
| **Authentication** | Control plane: session cookie from `POST /api/v1/auth/login`. Webhooks: HMAC signature. Worker: Google OIDC (cloud) or a shared token (local). |
| **CSRF** | Every `POST`/`PUT`/`PATCH`/`DELETE` on the control plane must send `X-CSRF-Token` (returned by login and `GET /auth/me`) and, if an `Origin` header is present, it must be allowlisted. |
| **Workspace** | Resources are scoped to the active workspace. Send `X-Workspace-Id` to choose one; the default is the caller's first membership. A workspace the caller does not belong to answers `404`. |
| **Correlation id** | Every response carries `X-Correlation-Id`. A caller may supply one (8–64 characters of `[A-Za-z0-9._-]`); otherwise one is generated. It is stored on the event and sent to the destination. |
| **Idempotency** | `POST /events/{id}/replay` requires an `Idempotency-Key` header (8–128 printable characters). |
| **Time** | RFC 3339 with offset. Query bounds without an offset are rejected. |
| **Identifiers** | UUIDs. |
| **Request bodies** | Unknown fields are rejected (`422`) so typos surface. |

### Errors

One envelope for every error, on every endpoint:

```json
{
  "error": {
    "code": "validation_failed",
    "message": "integration cannot be activated until these are fixed",
    "details": [{ "code": "invalid_path", "path": "mappings[0].source", "message": "empty path segment", "severity": "error" }],
    "correlation_id": "6f1c2b7e0a4d4d0f9c3b5a1e8d7f6a52"
  }
}
```

`code` is stable and meant for programs; `message` is for people. Messages never contain database
internals, stack traces, secret values, or the rejected input.

| Status | Codes |
|---|---|
| 400 | `bad_request`, `invalid_json`, `invalid_cursor` |
| 401 | `unauthenticated`, `invalid_credentials`, `invalid_signature` |
| 403 | `permission_denied`, `csrf_failed`, `origin_not_allowed`, `no_workspace` |
| 404 / 405 | `not_found`, `unknown_endpoint` / `method_not_allowed` |
| 409 | `conflict`, `not_replayable`, `idempotency_key_reused`, `invalid_status_change`, `invalid_state_transition`, `name_taken`, `connection_in_use`, `already_member`, `last_admin`, `already_decided`, `integration_not_active`, `payload_purged` |
| 413 / 415 | `payload_too_large` / `unsupported_media_type` |
| 422 | `validation_failed`, `invalid_request`, `no_matching_integration`, `unknown_adapter`, `idempotency_key_required`, `invalid_idempotency_key`, `weak_password`, `invalid_email`, `password_required` |
| 429 | `rate_limited` (with `Retry-After`) |
| 500 / 503 | `internal_error` / `service_unavailable`, `secret_unavailable` |
| other | `http_error` (a framework-level HTTP error with no more specific code) |

Entries in `details[]` carry their own finer-grained `code` (for example `invalid_path`,
`unmapped_required_field`, `destination_not_allowed`, `secret_in_header`) alongside the `path` they
refer to.

### Pagination

List endpoints return `{ "items": [...], "next_cursor": "…" | null }`. Pass `next_cursor` back as
`?cursor=`. `limit` is 1–100 (default 25). Ordering is newest first and stable; cursors are opaque.

## Webhook ingestion

```
POST /webhooks/{connection_id}
Content-Type: application/json
X-Relay-Timestamp: 1772359200
X-Relay-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>." + raw body>
X-Relay-Event-Id: evt_123            (optional; or configure a JSON path)
X-Relay-Event-Type: order.created    (optional)
```

Header names and whether a timestamp is required are per-connection settings. The body must be a
JSON object of at most 256 KiB.

| Response | Meaning |
|---|---|
| `202` | Stored durably. `results[]` has one entry per matching integration with `outcome` `accepted`, `duplicate` or `held`. |
| `422` with `results[]` | Stored, but rejected by input validation for every matching integration. `issues[]` says what and where. |
| `422 no_matching_integration` | No active integration accepts this event type. Nothing stored. |
| `401`, `413`, `415`, `400`, `429` | Rejected at the edge. Nothing stored. |

`202` means *durably accepted*, not *delivered*. Re-sending the same event id is safe.

## Control plane

| Method and path | Permission | Notes |
|---|---|---|
| `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` · `POST /auth/password` | — | `me` returns workspaces, role, permissions and the CSRF token. |
| `GET /workspaces` | signed in | |
| `GET /members` · `POST /members` · `PATCH /members/{user_id}` · `DELETE /members/{user_id}` | `member:read` / `member:manage` | A workspace always keeps one administrator. |
| `GET /adapters` | `connection:read` | Registered adapter types. |
| `GET/POST /connections` · `GET/PATCH/DELETE /connections/{id}` | `connection:read` / `connection:write` | Config is validated by the adapter; secrets are references only. |
| `GET/POST /integrations` · `GET/PATCH /integrations/{id}` | `integration:read` / `integration:write` | A configuration change creates a new version. |
| `GET /integrations/{id}/versions` | `integration:read` | Immutable history. |
| `POST /integrations/{id}/validate` | `integration:read` | Body `{config?}` validates a draft without saving. |
| `POST /integrations/preview` | `integration:read` | Dry-run `{config, payload}`; stores and delivers nothing. |
| `POST /integrations/{id}/activate` · `/disable` | `integration:write` | Activation runs validation. |
| `POST /integrations/{id}/pause` · `/resume` | `integration:operate` | Returns `held_events_affected`. |
| `GET /events` | `event:read` | Filters: `status` (repeatable), `integration_id`, `received_after`, `received_before`, `q` (exact id match). |
| `GET /events/{id}` · `GET /events/{id}/attempts` | `event:read` | Payloads are always redacted. |
| `POST /events/{id}/replay` | `event:replay` | Body `{reason, version_mode}`; `202`; `created: false` on a repeated key. |
| `POST /events/{id}/investigations` | `investigation:run` | Synchronous and bounded; `201` with the report. |
| `GET /investigations` · `GET /investigations/{id}` | `investigation:read` | |
| `POST /investigations/{id}/recommendations/{rec_id}/decision` | `recommendation:decide` | Records and audits; executes nothing. |
| `GET /ai/status` | `investigation:read` | Which provider slots are configured. Never returns credentials. |
| `GET /dashboard/summary?window=1h\|24h\|7d\|30d` | `event:read` | Computed from stored rows. |
| `GET /audit-logs` | `audit:read` | Filters: `action` (prefix), `entity_type`, `entity_id`. |
| `GET/PUT /demo/destination` · `GET /demo/destination/received` · `GET /simulator/scenarios` · `POST /simulator/send` | `event:read` / `demo:use` | Demo tooling. |

All paths above are relative to `/api/v1`.

## Worker (internal)

| Path | Caller | Behaviour |
|---|---|---|
| `POST /internal/pubsub/push` | Pub/Sub push subscription | Runs one delivery attempt. `2xx` = acknowledged (including a recorded destination failure and a discarded poison message); `503` = redeliver. |
| `POST /internal/pubsub/dead-letter` | Dead-letter push subscription | Marks the event dead-lettered when the broker has given up. |
| `POST /internal/outbox/dispatch` | Cloud Scheduler | Publishes due outbox rows, recovers expired leases, runs housekeeping. Bounded to about 20 seconds. |

The message body is a standard Pub/Sub push envelope whose `data` decodes to
`{"v": 1, "event_id", "workspace_id", "cycle", "attempt", "correlation_id"}`.

## What Relay sends to a destination

```
POST <configured url>
Content-Type: application/json
Idempotency-Key: <event id>             (header name configurable; stable across retries and replays)
X-Relay-Event-Id: <event id>
X-Relay-Correlation-Id: <correlation id>
X-Relay-Attempt: <n>
Authorization: Bearer <secret>          (or a custom header / basic auth, per connection)
```

Body: the transformed payload. A `2xx` is success. Redirects are not followed.

## Versioning

The control plane is versioned in the path. Additive changes (new fields, new endpoints, new error
codes) may be made within `/api/v1`; removing or changing the meaning of a field requires `/api/v2`.
The queue message carries its own version field (`v`).
