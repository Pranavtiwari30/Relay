/**
 * The one place the UI talks to the API.
 *
 * - Requests are same-origin (the dev server and the production host both proxy
 *   /api), so the HttpOnly session cookie is first-party and never visible here.
 * - State-changing requests carry the CSRF token the server issued at sign-in.
 * - Every failure becomes an ApiError with the server's stable error code and
 *   correlation id, so screens can show something actionable.
 */

const BASE = import.meta.env.VITE_API_BASE_URL ?? "";

export interface ErrorDetail {
  code?: string;
  path?: string;
  message?: string;
  severity?: string;
  [key: string]: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: ErrorDetail[];
  readonly correlationId: string | null;
  readonly retryAfterSeconds: number | null;

  constructor(
    status: number,
    code: string,
    message: string,
    details: ErrorDetail[] = [],
    correlationId: string | null = null,
    retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.correlationId = correlationId;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }
}

interface ClientState {
  csrfToken: string | null;
  workspaceId: string | null;
  onUnauthenticated: (() => void) | null;
}

const state: ClientState = { csrfToken: null, workspaceId: null, onUnauthenticated: null };

export function configureClient(next: Partial<ClientState>): void {
  Object.assign(state, next);
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | string[] | null | undefined>;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** Sign-in itself must not trigger the "session expired" handler. */
  skipAuthHandler?: boolean;
}

function buildUrl(path: string, query: RequestOptions["query"]): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) value.forEach((item) => params.append(key, item));
    else params.append(key, String(value));
  }
  const suffix = params.toString();
  return `${BASE}${path}${suffix ? `?${suffix}` : ""}`;
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.csrfToken) headers["X-CSRF-Token"] = state.csrfToken;
  if (state.workspaceId) headers["X-Workspace-Id"] = state.workspaceId;

  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), {
      method,
      headers,
      credentials: "include",
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(
      0,
      "network_error",
      "Could not reach the Relay API. Check your connection and try again.",
    );
  }

  if (response.status === 204) return undefined as T;

  const correlationId = response.headers.get("x-correlation-id");
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const envelope = (payload as { error?: Partial<ApiError> & { correlation_id?: string } } | null)?.error;
    const retryAfter = Number(response.headers.get("retry-after"));
    const error = new ApiError(
      response.status,
      envelope?.code ?? "http_error",
      envelope?.message ?? `The request failed (HTTP ${response.status}).`,
      (envelope?.details as ErrorDetail[] | undefined) ?? [],
      envelope?.correlation_id ?? correlationId,
      Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
    );
    if (response.status === 401 && !options.skipAuthHandler) state.onUnauthenticated?.();
    throw error;
  }
  return payload as T;
}

/** A key that makes a retried mutation safe: the server replays, at most, once per key. */
export function newIdempotencyKey(): string {
  return `ui-${crypto.randomUUID()}`;
}
