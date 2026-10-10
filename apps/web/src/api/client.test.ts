import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, configureClient, newIdempotencyKey } from "./client";

function respond(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

describe("api client", () => {
  const fetchMock = vi.fn<typeof fetch>();
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    configureClient({ csrfToken: "csrf-123", workspaceId: "ws-1", onUnauthenticated: null });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  function lastCall() {
    const call = fetchMock.mock.calls.at(-1);
    if (!call) throw new Error("fetch was not called");
    return { url: String(call[0]), init: call[1] as RequestInit & { headers: Record<string, string> } };
  }

  it("sends cookies and the workspace on reads, but no CSRF token", async () => {
    fetchMock.mockResolvedValue(respond(200, { ok: true }));
    await api("/api/v1/events", {
      query: { status: ["DELIVERED", "QUEUED"], q: "", cursor: null, limit: 25 },
    });
    const { url, init } = lastCall();
    expect(url).toBe("/api/v1/events?status=DELIVERED&status=QUEUED&limit=25");
    expect(init.credentials).toBe("include");
    expect(init.headers["X-Workspace-Id"]).toBe("ws-1");
    expect(init.headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("adds the CSRF token and a JSON body to state-changing requests", async () => {
    fetchMock.mockResolvedValue(respond(200, {}));
    await api("/api/v1/integrations/1/pause", {
      method: "POST",
      body: { a: 1 },
      headers: { "Idempotency-Key": "k" },
    });
    const { init } = lastCall();
    expect(init.headers["X-CSRF-Token"]).toBe("csrf-123");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(init.headers["Idempotency-Key"]).toBe("k");
    expect(init.body).toBe('{"a":1}');
  });

  it("turns the error envelope into a typed ApiError", async () => {
    fetchMock.mockResolvedValue(
      respond(
        422,
        {
          error: {
            code: "validation_failed",
            message: "not valid",
            details: [{ path: "a", message: "bad" }],
            correlation_id: "corr-9",
          },
        },
        { "retry-after": "30" },
      ),
    );
    const error = await api("/x", { method: "POST" }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(422);
    expect(apiError.code).toBe("validation_failed");
    expect(apiError.details).toEqual([{ path: "a", message: "bad" }]);
    expect(apiError.correlationId).toBe("corr-9");
    expect(apiError.retryAfterSeconds).toBe(30);
    expect(apiError.isForbidden).toBe(false);
  });

  it("falls back to the response header and a generic message when there is no envelope", async () => {
    fetchMock.mockResolvedValue(
      new Response("<html>bad gateway</html>", { status: 502, headers: { "x-correlation-id": "corr-h" } }),
    );
    const error = (await api("/x").catch((e: unknown) => e)) as ApiError;
    expect(error.code).toBe("http_error");
    expect(error.message).toContain("502");
    expect(error.correlationId).toBe("corr-h");
  });

  it("reports an expired session once, except during sign-in", async () => {
    const onUnauthenticated = vi.fn();
    configureClient({ onUnauthenticated });
    fetchMock.mockResolvedValue(respond(401, { error: { code: "unauthenticated", message: "sign in" } }));
    await expect(api("/api/v1/events")).rejects.toMatchObject({ status: 401 });
    expect(onUnauthenticated).toHaveBeenCalledTimes(1);
    await expect(api("/api/v1/auth/login", { method: "POST", skipAuthHandler: true })).rejects.toMatchObject({
      status: 401,
    });
    expect(onUnauthenticated).toHaveBeenCalledTimes(1);
  });

  it("maps a network failure to a readable error and returns undefined for 204", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await expect(api("/x")).rejects.toMatchObject({ status: 0, code: "network_error" });
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(api("/x", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("generates distinct idempotency keys", () => {
    expect(newIdempotencyKey()).toMatch(/^ui-[0-9a-f-]{36}$/);
    expect(newIdempotencyKey()).not.toBe(newIdempotencyKey());
  });
});
