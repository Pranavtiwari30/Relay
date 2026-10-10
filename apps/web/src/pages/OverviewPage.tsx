import { useState } from "react";
import { Link } from "react-router-dom";
import { useDashboard } from "../api/hooks";
import type { Dashboard, DashboardWindow } from "../api/types";
import { useAuth } from "../auth";
import { TrendChart } from "../components/TrendChart";
import {
  Card,
  EmptyState,
  IntegrationStatusBadge,
  PageHeader,
  QueryState,
  StatusBadge,
  Time,
} from "../components/ui";
import { DASH, formatCount, formatDuration, formatPercent, formatSpan } from "../lib/format";

const WINDOWS: { id: DashboardWindow; label: string }[] = [
  { id: "1h", label: "1 hour" },
  { id: "24h", label: "24 hours" },
  { id: "7d", label: "7 days" },
  { id: "30d", label: "30 days" },
];

function Tile({ label, value, note, to }: { label: string; value: string; note?: string; to?: string }) {
  const body = (
    <>
      <span className="tile-label">{label}</span>
      <span className="tile-value">{value}</span>
      {note && <span className="tile-note">{note}</span>}
    </>
  );
  return to ? (
    <Link className="tile" to={to} style={{ textDecoration: "none" }}>
      {body}
    </Link>
  ) : (
    <div className="tile">{body}</div>
  );
}

function Summary({ data }: { data: Dashboard }) {
  const { can } = useAuth();
  const { events, attempts, live } = data;
  const outcomes = events.delivered + events.failed;

  if (events.received === 0 && live.retry_scheduled === 0 && live.held === 0) {
    return (
      <Card>
        <EmptyState title="No events in this period">
          {can("demo:use") ? (
            <>
              Send a test event from <Link to="/settings?tab=demo">Settings → Demo tools</Link>, or POST to a
              source connection’s webhook URL.
            </>
          ) : (
            "Events will appear here once a source system starts sending them."
          )}
        </EmptyState>
      </Card>
    );
  }

  return (
    <>
      <section aria-label="Event totals" className="tiles">
        <Tile label="Received" value={formatCount(events.received)} to="/events" />
        <Tile label="Delivered" value={formatCount(events.delivered)} to="/events?status=DELIVERED" />
        <Tile
          label="Failed"
          value={formatCount(events.failed)}
          note={`${events.dead_lettered} dead-lettered · ${events.validation_failed} invalid`}
          to="/failures"
        />
        <Tile
          label="In flight"
          value={formatCount(events.in_flight)}
          note={events.filtered ? `${events.filtered} filtered out` : undefined}
        />
        <Tile
          label="Success rate"
          value={formatPercent(events.success_rate)}
          note={outcomes === 0 ? "No finished events yet" : `of ${formatCount(outcomes)} finished events`}
        />
        <Tile
          label="Delivery time (p50 / p95)"
          value={attempts.duration_ms_p50 === null ? DASH : formatDuration(attempts.duration_ms_p50)}
          note={
            attempts.duration_ms_p95 === null
              ? "No completed attempts"
              : `p95 ${formatDuration(attempts.duration_ms_p95)} · ${attempts.total} attempts`
          }
        />
      </section>

      <Card title="Needs attention now">
        <dl className="kv">
          <dt>Retries scheduled</dt>
          <dd>{live.retry_scheduled}</dd>
          <dt>Held (integration paused)</dt>
          <dd>{live.held}</dd>
          <dt>Oldest event still in flight</dt>
          <dd>
            {live.oldest_in_flight_age_seconds === null
              ? "None in flight"
              : formatSpan(live.oldest_in_flight_age_seconds)}
          </dd>
          <dt>Oldest unpublished queue message</dt>
          <dd>
            {live.oldest_pending_outbox_age_seconds === null
              ? "Nothing waiting"
              : `${formatSpan(live.oldest_pending_outbox_age_seconds)} — the dispatcher may be behind`}
          </dd>
        </dl>
        <p className="field-hint" style={{ marginTop: 8 }}>
          These are current values and ignore the time filter: stuck work stays visible however old it is.
        </p>
      </Card>

      {data.series.points.length > 1 && (
        <Card title={`Events per ${data.series.bucket}`}>
          <TrendChart points={data.series.points} bucket={data.series.bucket} />
        </Card>
      )}

      <div className="grid-2">
        <Card title="Integration health" flush>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Integration</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="num">
                    Delivered
                  </th>
                  <th scope="col" className="num">
                    Failed
                  </th>
                  <th scope="col" className="num">
                    Success
                  </th>
                  <th scope="col">Last event</th>
                </tr>
              </thead>
              <tbody>
                {data.integrations.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <Link to={`/integrations/${row.id}`}>{row.name}</Link>
                    </td>
                    <td>
                      <IntegrationStatusBadge status={row.status} />
                    </td>
                    <td className="num">{row.delivered}</td>
                    <td className="num">{row.failed}</td>
                    <td className="num">{formatPercent(row.success_rate)}</td>
                    <td>
                      <Time iso={row.last_event_at} relative />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title="Recent failures" actions={<Link to="/failures">View all</Link>} flush>
          {data.recent_failures.length === 0 ? (
            <EmptyState title="No failures" />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Event</th>
                    <th scope="col">Status</th>
                    <th scope="col">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent_failures.map((event) => (
                    <tr key={event.id}>
                      <td>
                        <Link to={`/events/${event.id}`} className="mono">
                          {event.source_event_id ?? event.id.slice(0, 8)}
                        </Link>
                        <div className="muted">
                          <Time iso={event.updated_at} relative />
                        </div>
                      </td>
                      <td>
                        <StatusBadge status={event.status} />
                      </td>
                      <td>{event.last_error ?? DASH}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}

export function OverviewPage() {
  const [window, setWindow] = useState<DashboardWindow>("24h");
  const query = useDashboard(window);
  return (
    <>
      <PageHeader
        title="Overview"
        description="Delivery health for this workspace, computed from stored events."
        actions={
          <div className="segmented" role="group" aria-label="Time range">
            {WINDOWS.map((w) => (
              <button key={w.id} type="button" aria-pressed={window === w.id} onClick={() => setWindow(w.id)}>
                {w.label}
              </button>
            ))}
          </div>
        }
      />
      <QueryState query={query}>{(data) => <Summary data={data} />}</QueryState>
    </>
  );
}
