import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Dashboard, EventDetail, EventStatus } from "../api/types";
import { App } from "../App";
import { TrendChart } from "../components/TrendChart";
import { StatusBadge } from "../components/ui";
import { apiError, mockApi, renderRoute, sessionFor } from "../test/harness";
import { EventDetailPage } from "./EventDetailPage";
import { EventsPage } from "./EventsPage";
import { OverviewPage } from "./OverviewPage";

afterEach(() => vi.unstubAllGlobals());

function dashboard(overrides: Partial<Dashboard> = {}): Dashboard {
  return {
    window: "24h",
    since: "2026-03-01T00:00:00Z",
    generated_at: "2026-03-02T00:00:00Z",
    is_demo_workspace: false,
    events: {
      received: 0,
      delivered: 0,
      failed: 0,
      in_flight: 0,
      filtered: 0,
      held: 0,
      dead_lettered: 0,
      validation_failed: 0,
      by_status: {},
      success_rate: null,
    },
    attempts: { total: 0, failed: 0, retries: 0, duration_ms_p50: null, duration_ms_p95: null },
    live: {
      retry_scheduled: 0,
      held: 0,
      oldest_in_flight_age_seconds: null,
      oldest_pending_outbox_age_seconds: null,
    },
    recent_failures: [],
    integrations: [],
    series: { bucket: "hour", points: [] },
    investigations: { total: 0, ai: 0, fell_back: 0, failed: 0, avg_duration_ms: null },
    ...overrides,
  };
}

function event(status: EventStatus, overrides: Partial<EventDetail> = {}): EventDetail {
  return {
    id: "6dc34399-5b2a-4445-9b83-6f3e5e66df1a",
    integration_id: "int-1",
    integration_name: "Orders",
    status,
    event_type: "order.created",
    source_event_id: "evt_abc",
    correlation_id: "corr-1",
    attempt_count: 3,
    max_attempts: 3,
    delivery_cycle: 1,
    next_attempt_at: null,
    failure_stage: status === "DELIVERED" ? null : "delivery",
    failure_category: status === "DELIVERED" ? null : "http_5xx",
    last_error: status === "DELIVERED" ? null : "destination responded with HTTP 503",
    received_at: "2026-03-01T10:00:00Z",
    updated_at: "2026-03-01T10:00:09Z",
    delivered_at: null,
    dead_lettered_at: null,
    integration_version_id: "ver-1",
    integration_version: 2,
    payload: { order_no: "A-1", payment: { card_number: "[REDACTED]" } },
    transformed_payload: { order_id: "A-1" },
    payload_size_bytes: 120,
    payload_purged: false,
    failure_details: null,
    replayable: status === "DEAD_LETTERED" || status === "VALIDATION_FAILED",
    transitions: [
      {
        id: 1,
        from_status: null,
        to_status: "RECEIVED",
        reason: "received",
        actor_type: "ingestion",
        actor_user_id: null,
        details: null,
        created_at: "2026-03-01T10:00:00Z",
      },
    ],
    replays: [],
    ...overrides,
  };
}

const eventRoutes = (detail: EventDetail) => ({
  [`GET /api/v1/events/${detail.id}`]: () => ({ body: detail }),
  [`GET /api/v1/events/${detail.id}/attempts`]: () => ({ body: [] }),
  "GET /api/v1/investigations": () => ({ body: { items: [], next_cursor: null } }),
});
const eventPage = { path: "/events/:eventId", route: "/events/6dc34399-5b2a-4445-9b83-6f3e5e66df1a" };

describe("app shell", () => {
  it("shows the sign-in form when there is no session", async () => {
    mockApi(null);
    renderRoute(<App />, { path: "*" });
    expect(await screen.findByRole("heading", { name: "Sign in to Relay" })).toBeInTheDocument();
  });

  it("explains bad credentials and rate limiting in plain language", async () => {
    const user = userEvent.setup();
    let reply = apiError(401, "invalid_credentials", "invalid email or password");
    mockApi(null, { "POST /api/v1/auth/login": () => reply });
    renderRoute(<App />, { path: "*" });
    await user.type(await screen.findByLabelText("Email"), "a@b.test");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("don’t match an account");

    reply = {
      ...apiError(429, "rate_limited", "too many"),
      headers: { "retry-after": "600" },
    } as typeof reply;
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Too many sign-in attempts. Try again in about 10 minute(s).",
      ),
    );
  });

  it("labels a demo workspace on every page", async () => {
    const session = sessionFor("viewer", {
      workspaces: [{ id: "ws-1", name: "Demo workspace", slug: "demo", is_demo: true, role: "viewer" }],
    });
    mockApi(session, { "GET /api/v1/dashboard/summary": () => ({ body: dashboard() }) });
    renderRoute(<App />, { path: "*" });
    expect(await screen.findByRole("note")).toHaveTextContent("Demo workspace.");
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
  });
});

describe("overview", () => {
  it("shows an empty state instead of zeros when nothing has happened", async () => {
    mockApi(sessionFor("viewer"), { "GET /api/v1/dashboard/summary": () => ({ body: dashboard() }) });
    renderRoute(<OverviewPage />);
    expect(await screen.findByText("No events in this period")).toBeInTheDocument();
    expect(screen.queryByText("Success rate")).not.toBeInTheDocument();
    // A viewer cannot use the simulator, so they are not pointed at it.
    expect(screen.queryByRole("link", { name: /Demo tools/ })).not.toBeInTheDocument();
  });

  it("renders a dash, never 0% or 100%, when no event has finished", async () => {
    const data = dashboard({ events: { ...dashboard().events, received: 4, in_flight: 4 } });
    mockApi(sessionFor("operator"), { "GET /api/v1/dashboard/summary": () => ({ body: data }) });
    renderRoute(<OverviewPage />);
    const tile = (await screen.findByText("Success rate")).closest(".tile") as HTMLElement;
    expect(within(tile).getByText("—")).toBeInTheDocument();
    expect(within(tile).getByText("No finished events yet")).toBeInTheDocument();
    const duration = screen.getByText("Delivery time (p50 / p95)").closest(".tile") as HTMLElement;
    expect(within(duration).getByText("No completed attempts")).toBeInTheDocument();
    expect(screen.getByText("None in flight")).toBeInTheDocument();
  });

  it("shows computed figures and refetches when the time range changes", async () => {
    const data = dashboard({
      events: {
        ...dashboard().events,
        received: 10,
        delivered: 7,
        failed: 3,
        dead_lettered: 2,
        validation_failed: 1,
        success_rate: 0.7,
      },
      attempts: { total: 19, failed: 9, retries: 6, duration_ms_p50: 3, duration_ms_p95: 4 },
    });
    const api = mockApi(sessionFor("operator"), { "GET /api/v1/dashboard/summary": () => ({ body: data }) });
    renderRoute(<OverviewPage />);
    expect(await screen.findByText("70.0%")).toBeInTheDocument();
    expect(screen.getByText("of 10 finished events")).toBeInTheDocument();
    expect(screen.getByText("2 dead-lettered · 1 invalid")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "7 days" }));
    expect(screen.getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");
    // The new range is requested from the server; figures are never recomputed client-side.
    await waitFor(() => expect(api.find("GET", "/api/v1/dashboard/summary").at(-1)?.query.window).toBe("7d"));
  });
});

describe("events list states", () => {
  const routes = { "GET /api/v1/integrations": () => ({ body: [] }) };

  it("shows loading, then an empty state", async () => {
    mockApi(sessionFor("viewer"), {
      ...routes,
      "GET /api/v1/events": () => ({ body: { items: [], next_cursor: null } }),
    });
    renderRoute(<EventsPage />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(await screen.findByText("No events yet")).toBeInTheDocument();
  });

  it("shows a server error with its reference and a retry", async () => {
    mockApi(sessionFor("viewer"), {
      ...routes,
      "GET /api/v1/events": () => apiError(500, "internal_error", "an unexpected error occurred"),
    });
    renderRoute(<EventsPage />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Something went wrong");
    expect(alert).toHaveTextContent("corr-test");
    expect(within(alert).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("shows a permission message, not a generic failure, for 403", async () => {
    mockApi(sessionFor("viewer"), {
      ...routes,
      "GET /api/v1/events": () =>
        apiError(403, "permission_denied", "your role (viewer) does not allow this action"),
    });
    renderRoute(<EventsPage />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("You don’t have access to this");
    expect(within(alert).queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });
});

describe("event detail permissions", () => {
  it("hides replay and investigate from a viewer and says why", async () => {
    mockApi(sessionFor("viewer"), eventRoutes(event("DEAD_LETTERED")));
    renderRoute(<EventDetailPage />, eventPage);
    expect(
      await screen.findByText(
        "Your role can view this event but not replay it. Ask an operator or administrator.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Replay…" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Investigate" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Received from source")).toHaveTextContent("[REDACTED]");
  });

  it("disables replay for a delivered event with an explanation", async () => {
    mockApi(sessionFor("operator"), eventRoutes(event("DELIVERED")));
    renderRoute(<EventDetailPage />, eventPage);
    const button = await screen.findByRole("button", { name: "Replay…" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute(
      "title",
      expect.stringContaining("Only dead-lettered and validation-failed events can be replayed"),
    );
  });

  it("requires a reason, and a double submit sends one idempotency key", async () => {
    const user = userEvent.setup();
    const detail = event("DEAD_LETTERED");
    const api = mockApi(sessionFor("operator"), {
      ...eventRoutes(detail),
      [`POST /api/v1/events/${detail.id}/replay`]: () => apiError(503, "service_unavailable", "try again"),
    });
    renderRoute(<EventDetailPage />, eventPage);
    await user.click(await screen.findByRole("button", { name: "Replay…" }));
    const dialog = screen.getByRole("dialog", { hidden: true });
    const confirm = within(dialog).getByRole("button", { name: "Replay event", hidden: true });
    expect(confirm).toBeDisabled();
    expect(dialog).toHaveTextContent("recorded in the audit log under your name");

    await user.type(within(dialog).getByLabelText("Reason"), "Destination recovered");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await within(dialog).findByText("try again");
    await user.click(confirm);
    await waitFor(() => expect(api.find("POST", `/api/v1/events/${detail.id}/replay`)).toHaveLength(2));

    const [first, second] = api.find("POST", `/api/v1/events/${detail.id}/replay`);
    expect(first?.headers["Idempotency-Key"]).toMatch(/^ui-/);
    // The retry reuses the key, so the server cannot replay the event twice.
    expect(second?.headers["Idempotency-Key"]).toBe(first?.headers["Idempotency-Key"]);
    expect(first?.headers["X-CSRF-Token"]).toBe("csrf-test");
    expect(first?.body).toEqual({ reason: "Destination recovered", version_mode: "current" });
  });
});

describe("shared components", () => {
  it("every event status has a text label and an icon, not colour alone", () => {
    const statuses: EventStatus[] = [
      "RECEIVED",
      "HELD",
      "VALIDATION_FAILED",
      "FILTERED",
      "PENDING_PUBLISH",
      "QUEUED",
      "PROCESSING",
      "RETRY_SCHEDULED",
      "DELIVERED",
      "DEAD_LETTERED",
      "REPLAY_REQUESTED",
    ];
    for (const status of statuses) {
      const { container, unmount } = render(<StatusBadge status={status} />);
      const badge = container.querySelector(".badge") as HTMLElement;
      expect(badge.textContent?.trim().length).toBeGreaterThan(3);
      expect(badge.querySelector(".badge-icon")).toHaveAttribute("aria-hidden", "true");
      expect(badge).toHaveAttribute("title");
      unmount();
    }
  });

  it("the trend chart is readable without colour or a pointer", async () => {
    const points = [
      { at: "2026-03-01T10:00:00Z", received: 10, delivered: 7, failed: 2 },
      { at: "2026-03-01T11:00:00Z", received: 4, delivered: 4, failed: 0 },
    ];
    render(<TrendChart points={points} bucket="hour" />);
    expect(screen.getByRole("img", { name: /Events received per hour/ })).toBeInTheDocument();
    // A legend names every series.
    for (const label of ["Delivered", "Failed", "Other (in flight, held, filtered)"])
      expect(screen.getByText(label)).toBeInTheDocument();
    // Each column is a focusable target that states its values ("other" is derived: 10 - 7 - 2).
    expect(screen.getByLabelText(/10 received, 7 delivered, 2 failed, 1 other/)).toHaveAttribute(
      "tabindex",
      "0",
    );

    await userEvent.setup().click(screen.getByRole("button", { name: "View as table" }));
    const table = screen.getByRole("table", { name: "Events per hour" });
    const cells = within(table)
      .getAllByRole("cell")
      .map((cell) => cell.textContent);
    expect(cells).toEqual(["10", "7", "2", "1", "4", "4", "0", "0"]);
    expect(screen.getByRole("button", { name: "View as chart" })).toHaveAttribute("aria-pressed", "true");
  });
});
