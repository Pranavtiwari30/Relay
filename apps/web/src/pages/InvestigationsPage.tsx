import { Link, useParams, useSearchParams } from "react-router-dom";
import { useAiStatus, useAuditLog, useInvestigation, useInvestigations } from "../api/hooks";
import { InvestigationReport } from "../components/InvestigationReport";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadMore,
  Loading,
  PageHeader,
  QueryState,
  Time,
} from "../components/ui";
import { DASH, formatDuration, humanize } from "../lib/format";

export function InvestigationsPage() {
  const query = useInvestigations();
  const status = useAiStatus();
  const rows = query.data?.pages.flatMap((p) => p.items) ?? [];
  const configured = (status.data?.providers ?? []).filter((p) => p.configured);
  return (
    <>
      <PageHeader
        title="Investigations"
        description="Read-only diagnoses of individual events. Start one from an event’s page. An investigation never changes configuration and never replays anything."
      />
      <Card title="Diagnosis engine">
        {status.isPending ? (
          <Loading />
        ) : configured.length > 0 ? (
          <p>
            Model providers, tried in order:{" "}
            {configured.map((p) => `${p.name} (${p.model ?? "default"})`).join(" → ")}. If all fail, Relay
            falls back to its rule-based diagnosis.
          </p>
        ) : (
          <p>
            No model provider is configured, so investigations use Relay’s rule-based diagnosis. This is a
            supported mode, not an error: event processing and diagnosis both work without any AI service.
          </p>
        )}
      </Card>
      <Card flush>
        {query.isPending ? (
          <Loading />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState title="No investigations yet">Open a failed event and choose “Investigate”.</EmptyState>
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Requested</th>
                    <th scope="col">Event</th>
                    <th scope="col">Mode</th>
                    <th scope="col">Likely cause</th>
                    <th scope="col">Confidence</th>
                    <th scope="col" className="num">
                      Duration
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((inv) => (
                    <tr key={inv.id}>
                      <td>
                        <Link to={`/investigations/${inv.id}`}>
                          <Time iso={inv.created_at} />
                        </Link>
                      </td>
                      <td>
                        <Link to={`/events/${inv.event_id}`} className="mono">
                          {inv.event_id.slice(0, 8)}
                        </Link>
                      </td>
                      <td>
                        {inv.mode === "ai" ? (
                          <Badge tone="info">AI-assisted</Badge>
                        ) : (
                          <Badge>Rule-based</Badge>
                        )}
                        {inv.fallback_reason && <div className="muted">fell back</div>}
                      </td>
                      <td>{humanize(inv.report?.likely_cause.category)}</td>
                      <td>{inv.report ? humanize(inv.report.likely_cause.confidence) : DASH}</td>
                      <td className="num">{formatDuration(inv.duration_ms)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <LoadMore
              hasMore={query.hasNextPage}
              loading={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
            />
          </>
        )}
      </Card>
    </>
  );
}

export function InvestigationDetailPage() {
  const { investigationId = "" } = useParams();
  const query = useInvestigation(investigationId);
  return (
    <QueryState query={query}>
      {(investigation) => (
        <>
          <PageHeader
            breadcrumb={<Link to="/investigations">Investigations</Link>}
            title="Investigation"
            description={
              <>
                Of event{" "}
                <Link to={`/events/${investigation.event_id}`} className="mono">
                  {investigation.event_id}
                </Link>
              </>
            }
            actions={
              <Link className="btn" to={`/events/${investigation.event_id}`}>
                Open event
              </Link>
            }
          />
          <InvestigationReport investigation={investigation} />
        </>
      )}
    </QueryState>
  );
}

const AUDIT_FILTERS = [
  { value: "", label: "All actions" },
  { value: "integration.", label: "Integration changes" },
  { value: "connection.", label: "Connection changes" },
  { value: "member.", label: "Membership and roles" },
  { value: "event.replay", label: "Replays" },
  { value: "investigation.", label: "Investigations and decisions" },
  { value: "demo.", label: "Demo tooling" },
];

function describeDetails(details: Record<string, unknown>): string {
  return Object.entries(details)
    .filter(([, value]) => value !== null && value !== "" && typeof value !== "object")
    .map(([key, value]) => `${key.replace(/_/g, " ")}: ${String(value)}`)
    .join(" · ");
}

export function AuditPage() {
  const [params, setParams] = useSearchParams();
  const action = params.get("action") ?? "";
  const setAction = (value: string) => setParams(value ? { action: value } : {}, { replace: true });
  const query = useAuditLog(action);
  const rows = query.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <>
      <PageHeader
        title="Audit log"
        description="Who changed what, and when. Entries are written in the same transaction as the change they describe and cannot be edited."
        actions={
          <label className="row" style={{ gap: 6 }}>
            <span className="field-label">Show</span>
            <select value={action} onChange={(e) => setAction(e.target.value)} style={{ width: "auto" }}>
              {AUDIT_FILTERS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          </label>
        }
      />
      <Card flush>
        {query.isPending ? (
          <Loading />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState title="No audit entries match" />
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Actor</th>
                    <th scope="col">Action</th>
                    <th scope="col">Resource</th>
                    <th scope="col">Details</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((entry) => (
                    <tr key={entry.id}>
                      <td>
                        <Time iso={entry.created_at} />
                      </td>
                      <td>{entry.actor_email ?? <span className="muted">{entry.actor_type}</span>}</td>
                      <td>
                        <span className="chip">{entry.action}</span>
                      </td>
                      <td>
                        {entry.entity_type === "event" ? (
                          <Link to={`/events/${entry.entity_id}`} className="mono">
                            event {entry.entity_id.slice(0, 8)}
                          </Link>
                        ) : entry.entity_type === "integration" ? (
                          <Link to={`/integrations/${entry.entity_id}`} className="mono">
                            integration {entry.entity_id.slice(0, 8)}
                          </Link>
                        ) : entry.entity_type === "investigation" ? (
                          <Link to={`/investigations/${entry.entity_id}`} className="mono">
                            investigation {entry.entity_id.slice(0, 8)}
                          </Link>
                        ) : (
                          <span className="mono">
                            {entry.entity_type} {entry.entity_id.slice(0, 8)}
                          </span>
                        )}
                      </td>
                      <td className="secondary">{describeDetails(entry.details) || DASH}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <LoadMore
              hasMore={query.hasNextPage}
              loading={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
            />
          </>
        )}
      </Card>
    </>
  );
}
