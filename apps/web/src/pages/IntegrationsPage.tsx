import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  useConnections,
  useIntegration,
  useIntegrationAction,
  useIntegrationVersions,
  useIntegrations,
  useSaveIntegration,
} from "../api/hooks";
import type { IntegrationDetail } from "../api/types";
import { useAuth } from "../auth";
import { IntegrationEditor } from "../components/IntegrationEditor";
import {
  Card,
  CopyButton,
  Dialog,
  EmptyState,
  IntegrationStatusBadge,
  JsonView,
  MutationError,
  Notice,
  PageHeader,
  QueryState,
  Tabs,
  Time,
} from "../components/ui";

export function IntegrationsPage() {
  const { can } = useAuth();
  const query = useIntegrations();
  return (
    <>
      <PageHeader
        title="Integrations"
        description="Each integration routes events from one source connection, transforms them, and delivers them to one destination."
        actions={
          can("integration:write") && (
            <Link className="btn btn-primary" to="/integrations/new">
              New integration
            </Link>
          )
        }
      />
      <Card flush>
        <QueryState
          query={query}
          isEmpty={(rows) => rows.length === 0}
          empty={
            <EmptyState title="No integrations yet">
              {can("integration:write")
                ? "Create a source and a destination connection in Settings, then add an integration."
                : "An administrator has not configured any integrations."}
            </EmptyState>
          }
        >
          {(rows) => (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Name</th>
                    <th scope="col">Status</th>
                    <th scope="col">Source</th>
                    <th scope="col">Destination</th>
                    <th scope="col" className="num">
                      Version
                    </th>
                    <th scope="col">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.id}>
                      <td>
                        <Link to={`/integrations/${row.id}`}>{row.name}</Link>
                        {row.description && <div className="muted truncate">{row.description}</div>}
                      </td>
                      <td>
                        <IntegrationStatusBadge status={row.status} />
                      </td>
                      <td>{row.source_connection_name}</td>
                      <td>
                        {row.destination_connection_name}{" "}
                        <span className="chip">{row.destination_adapter_type}</span>
                      </td>
                      <td className="num">v{row.current_version}</td>
                      <td>
                        <Time iso={row.updated_at} relative />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </QueryState>
      </Card>
    </>
  );
}

export function NewIntegrationPage() {
  const navigate = useNavigate();
  const connections = useConnections();
  const save = useSaveIntegration();
  return (
    <>
      <PageHeader
        breadcrumb={<Link to="/integrations">Integrations</Link>}
        title="New integration"
        description="Saved as a draft. A draft receives no events until it passes validation and is activated."
      />
      <QueryState query={connections}>
        {(rows) => (
          <IntegrationEditor
            integration={null}
            connections={rows}
            saving={save.isPending}
            saveError={save.error}
            onCancel={() => void navigate("/integrations")}
            onSave={(input) =>
              save.mutate(input, { onSuccess: (created) => void navigate(`/integrations/${created.id}`) })
            }
          />
        )}
      </QueryState>
    </>
  );
}

const ACTION_COPY = {
  activate: {
    button: "Activate",
    title: "Activate this integration?",
    body: "Relay validates the configuration and its connections first. Once active, events sent to the source connection are accepted and delivered.",
  },
  pause: {
    button: "Pause",
    title: "Pause delivery?",
    body: "New and in-flight events are held, not dropped. Nothing is delivered until you resume.",
  },
  resume: {
    button: "Resume",
    title: "Resume delivery?",
    body: "Every held event is queued for delivery immediately.",
  },
  disable: {
    button: "Disable",
    title: "Disable this integration?",
    body: "The source stops accepting events for it, and any held or in-flight events are dead-lettered. They can be replayed if you activate it again.",
  },
} as const;
type Action = keyof typeof ACTION_COPY;

function actionsFor(status: IntegrationDetail["status"], can: (p: string) => boolean): Action[] {
  const actions: Action[] = [];
  if ((status === "draft" || status === "disabled") && can("integration:write")) actions.push("activate");
  if (status === "active" && can("integration:operate")) actions.push("pause");
  if (status === "paused" && can("integration:operate")) actions.push("resume");
  if (status !== "disabled" && can("integration:write")) actions.push("disable");
  return actions;
}

function Detail({ integration }: { integration: IntegrationDetail }) {
  const { can } = useAuth();
  const [tab, setTab] = useState<"overview" | "config" | "versions">("overview");
  const [pending, setPending] = useState<Action | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const connections = useConnections();
  const versions = useIntegrationVersions(integration.id);
  const action = useIntegrationAction(integration.id);
  const save = useSaveIntegration(integration.id);
  const source = connections.data?.find((c) => c.id === integration.source_connection_id);
  const webhookUrl = source?.webhook_path ? `${window.location.origin}${source.webhook_path}` : null;
  const canEdit = can("integration:write");

  return (
    <>
      <PageHeader
        breadcrumb={<Link to="/integrations">Integrations</Link>}
        title={
          <span className="row">
            {integration.name} <IntegrationStatusBadge status={integration.status} />
          </span>
        }
        description={integration.description || undefined}
        actions={actionsFor(integration.status, can).map((a) => (
          <button
            key={a}
            type="button"
            className={
              a === "disable"
                ? "btn btn-danger"
                : a === "activate" || a === "resume"
                  ? "btn btn-primary"
                  : "btn"
            }
            onClick={() => {
              action.reset();
              setPending(a);
            }}
          >
            {ACTION_COPY[a].button}
          </button>
        ))}
      />
      {result && <Notice tone="good">{result}</Notice>}
      {integration.status === "paused" && (
        <Notice tone="warning">Paused: events are being accepted and held, not delivered.</Notice>
      )}
      {integration.status === "draft" && (
        <Notice>Draft: this integration does not receive events until it is activated.</Notice>
      )}

      <Tabs
        label="Integration sections"
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "overview", label: "Overview" },
          { id: "config", label: canEdit ? "Configuration" : "Configuration (read-only)" },
          { id: "versions", label: "Version history" },
        ]}
      />

      {tab === "overview" && (
        <div className="grid-2">
          <Card title="Route">
            <dl className="kv">
              <dt>Source</dt>
              <dd>{integration.source_connection_name}</dd>
              <dt>Webhook URL</dt>
              <dd>
                {webhookUrl ? (
                  <span className="row" style={{ gap: 8 }}>
                    <code>{webhookUrl}</code> <CopyButton value={webhookUrl} />
                  </span>
                ) : (
                  "—"
                )}
              </dd>
              <dt>Accepts event types</dt>
              <dd>
                {integration.config.event_types.length ? integration.config.event_types.join(", ") : "Any"}
              </dd>
              <dt>Destination</dt>
              <dd>
                {integration.destination_connection_name}{" "}
                <span className="chip">{integration.destination_adapter_type}</span>
              </dd>
              <dt>Configuration</dt>
              <dd>Version {integration.current_version}</dd>
            </dl>
          </Card>
          <Card title="Reliability settings">
            <dl className="kv">
              <dt>Delivery attempts</dt>
              <dd>Up to {integration.config.retry.max_attempts}, then dead-lettered</dd>
              <dt>Backoff</dt>
              <dd>
                Starts at {integration.config.retry.base_delay_seconds}s, doubles, capped at{" "}
                {integration.config.retry.max_delay_seconds}s, with jitter
              </dd>
              <dt>Timeout per attempt</dt>
              <dd>{integration.config.delivery.timeout_seconds}s</dd>
              <dt>Mappings</dt>
              <dd>
                {integration.config.passthrough
                  ? "Pass-through"
                  : `${integration.config.mappings.length} field mapping(s)`}
              </dd>
              <dt>Filters</dt>
              <dd>{integration.config.filters.length || "None"}</dd>
            </dl>
            <p style={{ marginTop: 12 }}>
              <Link to={`/events?integration_id=${integration.id}`}>View this integration’s events</Link>
            </p>
          </Card>
        </div>
      )}

      {tab === "config" &&
        (canEdit ? (
          <QueryState query={connections}>
            {(rows) => (
              <IntegrationEditor
                key={integration.current_version_id}
                integration={integration}
                connections={rows}
                saving={save.isPending}
                saveError={save.error}
                onSave={(input) =>
                  save.mutate(input, {
                    onSuccess: (next) =>
                      setResult(
                        `Saved as version ${next.current_version}. Events already accepted keep the version they arrived under.`,
                      ),
                  })
                }
              />
            )}
          </QueryState>
        ) : (
          <Card title={`Configuration — version ${integration.current_version}`}>
            <div className="stack">
              <Notice>Your role can view this configuration but not change it.</Notice>
              <JsonView value={integration.config} label="Current configuration" />
            </div>
          </Card>
        ))}

      {tab === "versions" && (
        <Card flush>
          <QueryState query={versions}>
            {(rows) => (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Version</th>
                      <th scope="col">Created</th>
                      <th scope="col">Change note</th>
                      <th scope="col">Configuration</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((v) => (
                      <tr key={v.id}>
                        <td className="nowrap">
                          v{v.version} {v.is_current && <span className="chip">current</span>}
                        </td>
                        <td>
                          <Time iso={v.created_at} />
                        </td>
                        <td>{v.change_note || <span className="muted">No note</span>}</td>
                        <td>
                          <details>
                            <summary>Show JSON</summary>
                            <JsonView value={v.config} label={`Version ${v.version}`} />
                          </details>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </QueryState>
        </Card>
      )}

      {pending && (
        <Dialog
          open
          title={ACTION_COPY[pending].title}
          onClose={() => setPending(null)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setPending(null)}>
                Cancel
              </button>
              <button
                type="button"
                className={pending === "disable" ? "btn btn-danger" : "btn btn-primary"}
                disabled={action.isPending}
                onClick={() =>
                  action.mutate(pending, {
                    onSuccess: (data) => {
                      setResult(
                        `Integration is now ${data.integration.status}.` +
                          (data.held_events_affected
                            ? ` ${data.held_events_affected} held event(s) were ${pending === "disable" ? "dead-lettered" : "released"}.`
                            : ""),
                      );
                      setPending(null);
                    },
                  })
                }
              >
                {action.isPending ? "Working…" : ACTION_COPY[pending].button}
              </button>
            </>
          }
        >
          <p>{ACTION_COPY[pending].body}</p>
          <MutationError error={action.error} />
        </Dialog>
      )}
    </>
  );
}

export function IntegrationDetailPage() {
  const { integrationId = "" } = useParams();
  const query = useIntegration(integrationId);
  return <QueryState query={query}>{(integration) => <Detail integration={integration} />}</QueryState>;
}
