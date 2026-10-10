import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useEvents, useIntegrations, type EventFilters } from "../api/hooks";
import type { EventStatus, EventSummary } from "../api/types";
import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  LoadMore,
  Loading,
  PageHeader,
  StatusBadge,
  Tabs,
  Time,
  eventStatusLabel,
} from "../components/ui";
import { DASH, humanize } from "../lib/format";

const STATUSES: EventStatus[] = [
  "DELIVERED",
  "DEAD_LETTERED",
  "VALIDATION_FAILED",
  "RETRY_SCHEDULED",
  "PROCESSING",
  "QUEUED",
  "PENDING_PUBLISH",
  "HELD",
  "FILTERED",
  "REPLAY_REQUESTED",
  "RECEIVED",
];

function EventTable({ events, showReason }: { events: EventSummary[]; showReason: boolean }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Received</th>
            <th scope="col">Status</th>
            <th scope="col">Integration</th>
            <th scope="col">Source event</th>
            <th scope="col" className="num">
              Attempts
            </th>
            <th scope="col">{showReason ? "Failure reason" : "Last error"}</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => (
            <tr key={event.id}>
              <td>
                <Link to={`/events/${event.id}`}>
                  <Time iso={event.received_at} />
                </Link>
              </td>
              <td>
                <StatusBadge status={event.status} />
              </td>
              <td>{event.integration_name ?? DASH}</td>
              <td>
                <span
                  className="mono truncate"
                  style={{ display: "inline-block" }}
                  title={event.source_event_id ?? undefined}
                >
                  {event.source_event_id ?? DASH}
                </span>
                {event.event_type && <div className="muted">{event.event_type}</div>}
              </td>
              <td className="num">
                {event.attempt_count}/{event.max_attempts}
                {event.delivery_cycle > 1 && <div className="muted">cycle {event.delivery_cycle}</div>}
              </td>
              <td>
                {event.last_error ?? DASH}
                {event.failure_stage && (
                  <div className="muted">
                    {humanize(event.failure_stage)}
                    {event.failure_category ? ` · ${humanize(event.failure_category)}` : ""}
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EventList({
  filters,
  empty,
  showReason = false,
}: {
  filters: EventFilters;
  empty: React.ReactNode;
  showReason?: boolean;
}) {
  const query = useEvents(filters);
  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const events = query.data.pages.flatMap((page) => page.items);
  if (events.length === 0) return <>{empty}</>;
  return (
    <>
      <EventTable events={events} showReason={showReason} />
      <LoadMore
        hasMore={query.hasNextPage}
        loading={query.isFetchingNextPage}
        onClick={() => void query.fetchNextPage()}
      />
    </>
  );
}

/** Owns its draft text; re-keyed by the applied query so it resets when the URL changes. */
function SearchBox({ initial, onSearch }: { initial: string; onSearch: (value: string) => void }) {
  const [draft, setDraft] = useState(initial);
  return (
    <form
      className="row"
      role="search"
      style={{ alignItems: "flex-end" }}
      onSubmit={(e) => {
        e.preventDefault();
        onSearch(draft.trim());
      }}
    >
      <Field label="Find by ID" hint="Exact event ID, source event ID, or correlation ID.">
        {(props) => (
          <input
            {...props}
            type="search"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="evt_… or UUID"
          />
        )}
      </Field>
      <div style={{ paddingBottom: 20 }}>
        <button className="btn" type="submit">
          Search
        </button>
      </div>
    </form>
  );
}

export function EventsPage() {
  const [params, setParams] = useSearchParams();
  const integrations = useIntegrations();
  const status = params.get("status") ?? "";
  const integrationId = params.get("integration_id") ?? "";
  const q = params.get("q") ?? "";

  function update(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  }

  const filtered = Boolean(status || integrationId || q);
  return (
    <>
      <PageHeader
        title="Events"
        description="Every event Relay has accepted, newest first. Filters are kept in the URL so a view can be shared."
      />
      <Card flush>
        <div className="card-body filters">
          <Field label="Status">
            {(props) => (
              <select {...props} value={status} onChange={(e) => update("status", e.target.value)}>
                <option value="">All statuses</option>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {eventStatusLabel(s)}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Integration">
            {(props) => (
              <select
                {...props}
                value={integrationId}
                onChange={(e) => update("integration_id", e.target.value)}
              >
                <option value="">All integrations</option>
                {(integrations.data ?? []).map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.name}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <SearchBox key={q} initial={q} onSearch={(value) => update("q", value)} />
          {filtered && (
            <div className="actions" style={{ paddingBottom: 20 }}>
              <button className="btn-link" type="button" onClick={() => setParams({}, { replace: true })}>
                Clear filters
              </button>
            </div>
          )}
        </div>
        <EventList
          filters={{ status: status ? [status] : [], integration_id: integrationId, q }}
          empty={
            <EmptyState title={filtered ? "No events match these filters" : "No events yet"}>
              {filtered
                ? "Try clearing a filter."
                : "Events appear here as soon as a source system sends one."}
            </EmptyState>
          }
        />
      </Card>
    </>
  );
}

type FailureTab = "dead" | "invalid" | "retrying";
const FAILURE_TABS: {
  id: FailureTab;
  label: string;
  statuses: EventStatus[];
  empty: string;
  help: string;
}[] = [
  {
    id: "dead",
    label: "Dead-lettered",
    statuses: ["DEAD_LETTERED"],
    empty: "No dead-lettered events",
    help: "Delivery failed permanently or ran out of attempts. Open an event to see why and to replay it.",
  },
  {
    id: "invalid",
    label: "Validation failed",
    statuses: ["VALIDATION_FAILED"],
    empty: "No validation failures",
    help: "Rejected by a schema or mapping before delivery. Fix the configuration or the source, then replay.",
  },
  {
    id: "retrying",
    label: "Retrying",
    statuses: ["RETRY_SCHEDULED"],
    empty: "Nothing is waiting to retry",
    help: "A delivery failed and another attempt is scheduled. No action is needed unless it keeps failing.",
  },
];

export function FailuresPage() {
  const [tab, setTab] = useState<FailureTab>("dead");
  const current = FAILURE_TABS.find((t) => t.id === tab);
  if (!current) return null;
  return (
    <>
      <PageHeader
        title="Failures"
        description="Events that need a decision. Replaying is always an explicit, audited action taken from the event’s page."
      />
      <Tabs tabs={FAILURE_TABS} value={tab} onChange={setTab} label="Failure type" />
      <p className="secondary">{current.help}</p>
      <Card flush>
        <EventList
          filters={{ status: current.statuses, integration_id: "", q: "" }}
          showReason
          empty={<EmptyState title={current.empty} />}
        />
      </Card>
    </>
  );
}
