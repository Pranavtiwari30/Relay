import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  newIdempotencyKey,
  useAttempts,
  useEvent,
  useInvestigations,
  useReplay,
  useRunInvestigation,
} from "../api/hooks";
import type { Attempt, EventDetail, Transition } from "../api/types";
import { useAuth } from "../auth";
import {
  Badge,
  Card,
  CopyButton,
  Dialog,
  EmptyState,
  Field,
  IssueList,
  JsonView,
  MutationError,
  Notice,
  PageHeader,
  QueryState,
  StatusBadge,
  Time,
  eventStatusLabel,
} from "../components/ui";
import { DASH, formatDuration, humanize } from "../lib/format";

const SETTLED = new Set(["DELIVERED", "FILTERED", "VALIDATION_FAILED", "DEAD_LETTERED", "HELD"]);

function dotFor(status: string): string {
  if (status === "DELIVERED") return "dot dot-good";
  if (status === "DEAD_LETTERED" || status === "VALIDATION_FAILED") return "dot dot-critical";
  if (status === "RETRY_SCHEDULED" || status === "HELD") return "dot dot-warning";
  return "dot dot-info";
}

type TimelineItem = { at: string; key: string } & (
  { kind: "transition"; row: Transition } | { kind: "attempt"; row: Attempt }
);

function Timeline({ event, attempts }: { event: EventDetail; attempts: Attempt[] }) {
  const items = useMemo<TimelineItem[]>(() => {
    const merged: TimelineItem[] = [
      ...event.transitions.map((row) => ({
        kind: "transition" as const,
        row,
        at: row.created_at,
        key: `t${row.id}`,
      })),
      ...attempts
        .filter((a) => a.finished_at)
        .map((row) => ({ kind: "attempt" as const, row, at: row.finished_at as string, key: `a${row.id}` })),
    ];
    // Stable order: by time, and an attempt's result before the transition it caused.
    return merged.sort((a, b) => a.at.localeCompare(b.at) || (a.kind === "attempt" ? -1 : 1));
  }, [event.transitions, attempts]);

  return (
    <ol className="timeline">
      {items.map((item) =>
        item.kind === "transition" ? (
          <li key={item.key}>
            <span className={dotFor(item.row.to_status)} aria-hidden="true" />
            <div>
              <div className="row" style={{ gap: 8 }}>
                <StatusBadge status={item.row.to_status} />
                <span className="secondary">{humanize(item.row.reason)}</span>
              </div>
              <div className="muted">
                <Time iso={item.row.created_at} /> · by {item.row.actor_type}
              </div>
            </div>
          </li>
        ) : (
          <li key={item.key}>
            <span
              className={item.row.status === "succeeded" ? "dot dot-good" : "dot dot-critical"}
              aria-hidden="true"
            />
            <div>
              <div>
                <strong>
                  Attempt {item.row.attempt_number}
                  {item.row.cycle > 1 ? ` (replay cycle ${item.row.cycle})` : ""}
                </strong>{" "}
                {item.row.status === "succeeded" ? "succeeded" : "failed"}
                {item.row.http_status ? ` — HTTP ${item.row.http_status}` : ""}
                {item.row.failure_category ? ` — ${humanize(item.row.failure_category)}` : ""}
              </div>
              <div className="muted">
                <Time iso={item.row.finished_at} /> · {formatDuration(item.row.duration_ms)}
              </div>
            </div>
          </li>
        ),
      )}
    </ol>
  );
}

function AttemptsTable({ attempts }: { attempts: Attempt[] }) {
  const [open, setOpen] = useState<string | null>(null);
  if (attempts.length === 0)
    return (
      <EmptyState title="No delivery attempts">This event has not been sent to the destination.</EmptyState>
    );
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Attempt</th>
            <th scope="col">Result</th>
            <th scope="col">HTTP</th>
            <th scope="col">Category</th>
            <th scope="col" className="num">
              Duration
            </th>
            <th scope="col">Started</th>
            <th scope="col">
              <span className="sr-only">Details</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {attempts.map((a) => (
            <FragmentRow
              key={a.id}
              attempt={a}
              open={open === a.id}
              onToggle={() => setOpen(open === a.id ? null : a.id)}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FragmentRow({
  attempt: a,
  open,
  onToggle,
}: {
  attempt: Attempt;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr>
        <td>
          #{a.attempt_number}
          {a.cycle > 1 && <span className="muted"> · cycle {a.cycle}</span>}
        </td>
        <td>
          {a.status === "succeeded" ? (
            <Badge tone="good" icon="✓">
              Succeeded
            </Badge>
          ) : a.status === "failed" ? (
            <Badge tone="critical" icon="✕">
              Failed
            </Badge>
          ) : (
            <Badge tone="info" icon="◐">
              In progress
            </Badge>
          )}
        </td>
        <td>{a.http_status ?? DASH}</td>
        <td>{humanize(a.failure_category)}</td>
        <td className="num">{formatDuration(a.duration_ms)}</td>
        <td>
          <Time iso={a.started_at} />
        </td>
        <td>
          <button type="button" className="btn btn-sm" aria-expanded={open} onClick={onToggle}>
            {open ? "Hide" : "Details"}
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={7}>
            <dl className="kv">
              <dt>Request</dt>
              <dd className="mono">
                {a.request_method ?? DASH} {a.request_url ?? ""}
              </dd>
              <dt>Idempotency key</dt>
              <dd className="mono">{a.idempotency_key}</dd>
              <dt>Error</dt>
              <dd>{a.error_message ?? DASH}</dd>
              {a.retry_after_seconds !== null && (
                <>
                  <dt>Retry-After</dt>
                  <dd>{a.retry_after_seconds}s</dd>
                </>
              )}
              <dt>Destination response</dt>
              <dd>
                {a.response_summary ? (
                  <>
                    <pre className="json" style={{ maxHeight: 180 }}>
                      {a.response_summary}
                    </pre>
                    <span className="field-hint">
                      Redacted and truncated. This text came from the destination and is shown as-is.
                    </span>
                  </>
                ) : (
                  DASH
                )}
              </dd>
            </dl>
          </td>
        </tr>
      )}
    </>
  );
}

function ReplayDialog({ event, onClose }: { event: EventDetail; onClose: () => void }) {
  const replay = useReplay(event.id);
  const [reason, setReason] = useState("");
  const [mode, setMode] = useState<"current" | "original">("current");
  // One key for the lifetime of this dialog: submitting twice replays once.
  const [idempotencyKey] = useState(newIdempotencyKey);
  const valid = reason.trim().length >= 3;
  return (
    <Dialog
      open
      title="Replay this event?"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!valid || replay.isPending}
            onClick={() =>
              replay.mutate(
                { reason: reason.trim(), version_mode: mode, idempotencyKey },
                { onSuccess: onClose },
              )
            }
          >
            {replay.isPending ? "Replaying…" : "Replay event"}
          </button>
        </>
      }
    >
      <p>
        Relay will send this event to the destination again in a new delivery cycle with a fresh attempt
        budget. Existing attempts and history are kept, and the replay is recorded in the audit log under your
        name.
      </p>
      <Notice tone="warning">
        If an earlier attempt timed out, the destination may already have processed it. Relay sends the same
        idempotency key (<code>{event.id}</code>) so a destination that honours it will not apply the event
        twice.
      </Notice>
      <Field label="Reason" hint="Recorded in the audit log.">
        {(props) => (
          <input
            {...props}
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={1000}
            autoFocus
          />
        )}
      </Field>
      <fieldset style={{ border: 0, padding: 0, margin: 0 }} className="stack-sm">
        <legend className="field-label">Configuration to use</legend>
        <label className="row" style={{ gap: 6 }}>
          <input type="radio" name="mode" checked={mode === "current"} onChange={() => setMode("current")} />
          Current version <span className="muted">— picks up any fixes made since</span>
        </label>
        <label className="row" style={{ gap: 6 }}>
          <input
            type="radio"
            name="mode"
            checked={mode === "original"}
            onChange={() => setMode("original")}
          />
          Original version (v{event.integration_version ?? "?"}){" "}
          <span className="muted">— exactly as first processed</span>
        </label>
      </fieldset>
      <MutationError error={replay.error} />
    </Dialog>
  );
}

function Detail({ event }: { event: EventDetail }) {
  const { can } = useAuth();
  const navigate = useNavigate();
  const live = !SETTLED.has(event.status);
  const attempts = useAttempts(event.id, event.updated_at, live);
  const investigations = useInvestigations(event.id);
  const investigate = useRunInvestigation(event.id);
  const [replaying, setReplaying] = useState(false);
  const past = investigations.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <>
      <PageHeader
        breadcrumb={<Link to="/events">Events</Link>}
        title={
          <span className="row">
            Event <span className="mono">{event.source_event_id ?? event.id.slice(0, 8)}</span>{" "}
            <StatusBadge status={event.status} />
          </span>
        }
        description={event.last_error ?? undefined}
        actions={
          <>
            {can("investigation:run") && (
              <button
                type="button"
                className="btn"
                disabled={investigate.isPending}
                onClick={() =>
                  investigate.mutate(undefined, {
                    onSuccess: (inv) => void navigate(`/investigations/${inv.id}`),
                  })
                }
              >
                {investigate.isPending ? "Investigating…" : "Investigate"}
              </button>
            )}
            {can("event:replay") && (
              <button
                type="button"
                className="btn btn-primary"
                disabled={!event.replayable}
                title={
                  event.replayable
                    ? undefined
                    : `Only dead-lettered and validation-failed events can be replayed (this one is ${eventStatusLabel(event.status)}).`
                }
                onClick={() => setReplaying(true)}
              >
                Replay…
              </button>
            )}
          </>
        }
      />
      <MutationError error={investigate.error} />
      {!can("event:replay") && event.replayable && (
        <Notice>Your role can view this event but not replay it. Ask an operator or administrator.</Notice>
      )}
      {event.status === "RETRY_SCHEDULED" && (
        <Notice tone="warning">
          The last delivery failed. Next attempt <Time iso={event.next_attempt_at} relative /> (attempt{" "}
          {event.attempt_count + 1} of {event.max_attempts}).
        </Notice>
      )}
      {event.failure_details && event.failure_details.length > 0 && (
        <Card title={`Validation issues — ${humanize(event.failure_stage)}`}>
          <IssueList issues={event.failure_details} />
        </Card>
      )}

      <div className="grid-2">
        <Card title="Details">
          <dl className="kv">
            <dt>Event ID</dt>
            <dd className="row" style={{ gap: 8 }}>
              <span className="mono">{event.id}</span> <CopyButton value={event.id} />
            </dd>
            <dt>Source event ID</dt>
            <dd className="mono">{event.source_event_id ?? DASH}</dd>
            <dt>Event type</dt>
            <dd>{event.event_type ?? DASH}</dd>
            <dt>Integration</dt>
            <dd>
              <Link to={`/integrations/${event.integration_id}`}>
                {event.integration_name ?? "Integration"}
              </Link>{" "}
              · configuration v{event.integration_version ?? "?"}
            </dd>
            <dt>Correlation ID</dt>
            <dd className="mono">{event.correlation_id}</dd>
            <dt>Received</dt>
            <dd>
              <Time iso={event.received_at} />
            </dd>
            <dt>Attempts</dt>
            <dd>
              {event.attempt_count} of {event.max_attempts}
              {event.delivery_cycle > 1 ? ` in replay cycle ${event.delivery_cycle}` : ""}
            </dd>
            <dt>Failure</dt>
            <dd>
              {event.failure_stage
                ? `${humanize(event.failure_stage)}${event.failure_category ? ` · ${humanize(event.failure_category)}` : ""}`
                : DASH}
            </dd>
          </dl>
        </Card>
        <Card title="Timeline">
          <Timeline event={event} attempts={attempts.data ?? []} />
        </Card>
      </div>

      <Card title="Delivery attempts" flush>
        <QueryState query={attempts}>{(rows) => <AttemptsTable attempts={rows} />}</QueryState>
      </Card>

      <Card title="Payloads">
        {event.payload_purged ? (
          <Notice>
            The stored payload was removed by the retention policy. History is kept; the event can no longer
            be replayed.
          </Notice>
        ) : (
          <div className="stack">
            <p className="field-hint">
              Sensitive fields are redacted for display. The stored original is used for delivery and replay.
            </p>
            <div className="grid-2">
              <JsonView value={event.payload} label="Received from source" />
              {event.transformed_payload ? (
                <JsonView value={event.transformed_payload} label="Sent to destination" />
              ) : (
                <div className="stack-sm">
                  <span className="field-label">Sent to destination</span>
                  <p className="muted">
                    Nothing was produced: the event did not pass validation and transformation.
                  </p>
                </div>
              )}
            </div>
          </div>
        )}
      </Card>

      {event.replays.length > 0 && (
        <Card title="Replays" flush>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Cycle</th>
                  <th scope="col">Requested</th>
                  <th scope="col">From status</th>
                  <th scope="col">Configuration</th>
                  <th scope="col">Reason</th>
                </tr>
              </thead>
              <tbody>
                {event.replays.map((r) => (
                  <tr key={r.id}>
                    <td>{r.cycle}</td>
                    <td>
                      <Time iso={r.created_at} />
                    </td>
                    <td>{humanize(r.from_status)}</td>
                    <td>{r.version_mode === "current" ? "Current at the time" : "Original"}</td>
                    <td>{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {past.length > 0 && (
        <Card title="Investigations" flush>
          <div className="table-wrap">
            <table>
              <tbody>
                {past.map((inv) => (
                  <tr key={inv.id}>
                    <td>
                      <Link to={`/investigations/${inv.id}`}>
                        <Time iso={inv.created_at} />
                      </Link>
                    </td>
                    <td>{inv.mode === "ai" ? `AI-assisted (${inv.provider})` : "Rule-based"}</td>
                    <td>{humanize(inv.report?.likely_cause.category)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
      {replaying && <ReplayDialog event={event} onClose={() => setReplaying(false)} />}
    </>
  );
}

export function EventDetailPage() {
  const { eventId = "" } = useParams();
  const query = useEvent(eventId);
  return <QueryState query={query}>{(event) => <Detail event={event} />}</QueryState>;
}
