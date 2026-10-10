import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import type { Role, Session } from "../api/types";
import { AuthProvider } from "../auth";

const PERMISSIONS: Record<Role, string[]> = {
  viewer: [
    "workspace:read",
    "member:read",
    "connection:read",
    "integration:read",
    "event:read",
    "investigation:read",
    "audit:read",
  ],
  operator: ["event:replay", "investigation:run", "recommendation:decide", "integration:operate", "demo:use"],
  admin: ["member:manage", "connection:write", "integration:write"],
};

export function sessionFor(role: Role, overrides: Partial<Session> = {}): Session {
  const permissions = [
    ...PERMISSIONS.viewer,
    ...(role !== "viewer" ? PERMISSIONS.operator : []),
    ...(role === "admin" ? PERMISSIONS.admin : []),
  ];
  return {
    user: { id: "u-1", email: `${role}@example.test`, display_name: role },
    workspaces: [{ id: "ws-1", name: "Acme", slug: "acme", is_demo: false, role }],
    active_workspace_id: "ws-1",
    role,
    permissions,
    csrf_token: "csrf-test",
    expires_at: "2099-01-01T00:00:00Z",
    ...overrides,
  };
}

export interface Recorded {
  method: string;
  path: string;
  query: Record<string, string>;
  headers: Record<string, string>;
  body: unknown;
}

interface Reply {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
}
type Handler = (request: Recorded) => Reply;

/**
 * Replace fetch with a small router. Keys are "METHOD /path" (exact) — query
 * strings are ignored for matching. Unmatched requests fail the test loudly.
 */
export function mockApi(session: Session | null, handlers: Record<string, Handler> = {}) {
  const calls: Recorded[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const record: Recorded = {
      method: init?.method ?? "GET",
      path: url.pathname,
      query: Object.fromEntries(url.searchParams),
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    };
    calls.push(record);
    const key = `${record.method} ${record.path}`;
    const me: Handler = () =>
      session
        ? { body: session }
        : { status: 401, body: { error: { code: "unauthenticated", message: "sign in to continue" } } };
    const handler: Handler | undefined = handlers[key] ?? (key === "GET /api/v1/auth/me" ? me : undefined);
    if (!handler) throw new Error(`Unhandled request in test: ${key}`);
    const reply = await handler(record);
    const status = reply.status ?? 200;
    return new Response(status === 204 ? null : JSON.stringify(reply.body ?? {}), {
      status,
      headers: { "content-type": "application/json", "x-correlation-id": "corr-test", ...reply.headers },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    calls,
    find: (method: string, path: string) => calls.filter((c) => c.method === method && c.path === path),
  };
}

export function renderRoute(
  element: ReactElement,
  { path = "/", route = "/" }: { path?: string; route?: string } = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <AuthProvider>
          <Routes>
            <Route path={path} element={element} />
            <Route path="*" element={<div>navigated away</div>} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

export function apiError(status: number, code: string, message: string) {
  return { status, body: { error: { code, message, details: [], correlation_id: "corr-test" } } };
}
