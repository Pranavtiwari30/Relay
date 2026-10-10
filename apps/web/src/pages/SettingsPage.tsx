import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  useAdapters,
  useConnections,
  useDeleteConnection,
  useDemoReceived,
  useDemoSetting,
  useMemberMutations,
  useMembers,
  useSaveConnection,
  useScenarios,
  useSetDemoMode,
  useSimulate,
} from "../api/hooks";
import type { Connection, DemoSetting, Role, SimulatorOutcome } from "../api/types";
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
  Tabs,
  Time,
} from "../components/ui";
import { humanize, parseJson } from "../lib/format";

type SettingsTab = "connections" | "members" | "demo";

const HTTP_TEMPLATE = `{
  "url": "https://api.example.com/v1/orders",
  "method": "POST",
  "auth": { "type": "bearer" }
}`;

function ConnectionDialog({ connection, onClose }: { connection: Connection | null; onClose: () => void }) {
  const adapters = useAdapters();
  const save = useSaveConnection(connection?.id);
  const [name, setName] = useState(connection?.name ?? "");
  const [direction, setDirection] = useState<"source" | "destination">(connection?.direction ?? "source");
  const [adapterType, setAdapterType] = useState(connection?.adapter_type ?? "webhook");
  const [configText, setConfigText] = useState(
    connection ? JSON.stringify(connection.config, null, 2) : "{}",
  );
  const [secretRef, setSecretRef] = useState(connection?.secret_ref ?? "");
  const [active, setActive] = useState(connection?.is_active ?? true);
  const parsed = parseJson(configText);
  const configOk =
    parsed.ok && typeof parsed.value === "object" && parsed.value !== null && !Array.isArray(parsed.value);
  const types = direction === "source" ? (adapters.data?.sources ?? []) : (adapters.data?.destinations ?? []);

  return (
    <Dialog
      open
      title={connection ? `Edit ${connection.name}` : "New connection"}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!name.trim() || !configOk || save.isPending}
            onClick={() =>
              save.mutate(
                {
                  name: name.trim(),
                  direction,
                  adapter_type: adapterType,
                  config: parsed.ok ? (parsed.value as Record<string, unknown>) : {},
                  secret_ref: secretRef.trim() || null,
                  is_active: active,
                },
                { onSuccess: onClose },
              )
            }
          >
            {save.isPending ? "Saving…" : "Save connection"}
          </button>
        </>
      }
    >
      <Field label="Name">
        {(p) => (
          <input {...p} type="text" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
        )}
      </Field>
      {!connection && (
        <div className="grid-2">
          <Field label="Direction">
            {(p) => (
              <select
                {...p}
                value={direction}
                onChange={(e) => {
                  const next = e.target.value as "source" | "destination";
                  setDirection(next);
                  setAdapterType(next === "source" ? "webhook" : "http");
                  setConfigText(next === "source" ? "{}" : HTTP_TEMPLATE);
                }}
              >
                <option value="source">Source (sends events to Relay)</option>
                <option value="destination">Destination (receives events)</option>
              </select>
            )}
          </Field>
          <Field label="Adapter">
            {(p) => (
              <select {...p} value={adapterType} onChange={(e) => setAdapterType(e.target.value)}>
                {types.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            )}
          </Field>
        </div>
      )}
      <Field
        label="Configuration (JSON)"
        hint="Never put credentials here. Use the secret reference below."
        error={configOk ? null : "Must be a JSON object."}
      >
        {(p) => (
          <textarea
            {...p}
            rows={8}
            spellCheck={false}
            value={configText}
            onChange={(e) => setConfigText(e.target.value)}
          />
        )}
      </Field>
      <Field
        label="Secret reference"
        hint={
          <>
            The <em>name</em> of an environment variable holding the secret, e.g.{" "}
            <code>env:RELAY_SECRET_ORDERS_WEBHOOK</code>. Relay stores this reference only, never the value.
          </>
        }
      >
        {(p) => (
          <input
            {...p}
            type="text"
            value={secretRef}
            onChange={(e) => setSecretRef(e.target.value)}
            placeholder="env:RELAY_SECRET_…"
            autoComplete="off"
          />
        )}
      </Field>
      {connection && (
        <label className="row" style={{ gap: 6 }}>
          <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /> Active
        </label>
      )}
      <MutationError error={save.error} />
    </Dialog>
  );
}

function Connections() {
  const { can } = useAuth();
  const query = useConnections();
  const remove = useDeleteConnection();
  const [editing, setEditing] = useState<Connection | "new" | null>(null);
  const writable = can("connection:write");
  return (
    <>
      <Card
        title="Connections"
        flush
        actions={
          writable && (
            <button type="button" className="btn btn-primary btn-sm" onClick={() => setEditing("new")}>
              New connection
            </button>
          )
        }
      >
        <QueryState
          query={query}
          isEmpty={(rows) => rows.length === 0}
          empty={<EmptyState title="No connections yet" />}
        >
          {(rows) => (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Name</th>
                    <th scope="col">Type</th>
                    <th scope="col">Endpoint</th>
                    <th scope="col">Secret</th>
                    <th scope="col">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c) => {
                    const endpoint = c.webhook_path
                      ? `${window.location.origin}${c.webhook_path}`
                      : typeof c.config.url === "string"
                        ? c.config.url
                        : "Built-in";
                    return (
                      <tr key={c.id}>
                        <td>
                          {c.name}
                          {!c.is_active && (
                            <div>
                              <Badge>Inactive</Badge>
                            </div>
                          )}
                          <IssueList issues={c.warnings} />
                        </td>
                        <td>
                          {humanize(c.direction)} <span className="chip">{c.adapter_type}</span>
                        </td>
                        <td>
                          <div className="row" style={{ gap: 6 }}>
                            <code className="truncate" title={endpoint}>
                              {endpoint}
                            </code>
                            {c.webhook_path && <CopyButton value={endpoint} />}
                          </div>
                        </td>
                        <td>
                          {c.secret_ref === null ? (
                            <span className="muted">None</span>
                          ) : (
                            <>
                              <code>{c.secret_ref}</code>
                              <div>
                                {c.secret_resolvable ? (
                                  <Badge tone="good" icon="✓">
                                    Available
                                  </Badge>
                                ) : (
                                  <Badge
                                    tone="critical"
                                    icon="✕"
                                    title="The environment variable is not set on the server."
                                  >
                                    Not set on server
                                  </Badge>
                                )}
                              </div>
                            </>
                          )}
                        </td>
                        <td>
                          {writable && (
                            <div className="actions">
                              <button type="button" className="btn btn-sm" onClick={() => setEditing(c)}>
                                Edit
                              </button>
                              <button
                                type="button"
                                className="btn btn-sm"
                                disabled={remove.isPending}
                                onClick={() => {
                                  if (window.confirm(`Delete connection “${c.name}”? This cannot be undone.`))
                                    remove.mutate(c.id);
                                }}
                              >
                                Delete
                              </button>
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </QueryState>
      </Card>
      <MutationError error={remove.error} />
      {!writable && <Notice>Only administrators can add or change connections.</Notice>}
      {editing && (
        <ConnectionDialog connection={editing === "new" ? null : editing} onClose={() => setEditing(null)} />
      )}
    </>
  );
}

function Members() {
  const { can, session } = useAuth();
  const query = useMembers();
  const { add, changeRole, remove } = useMemberMutations();
  const manage = can("member:manage");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [password, setPassword] = useState("");
  return (
    <>
      <Card title="Members" flush>
        <QueryState query={query}>
          {(rows) => (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Member</th>
                    <th scope="col">Role</th>
                    <th scope="col">Joined</th>
                    <th scope="col">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((m) => (
                    <tr key={m.user_id}>
                      <td>
                        {m.email} {m.user_id === session?.user.id && <span className="chip">you</span>}
                      </td>
                      <td>
                        {manage ? (
                          <select
                            aria-label={`Role for ${m.email}`}
                            value={m.role}
                            style={{ width: "auto" }}
                            disabled={changeRole.isPending}
                            onChange={(e) =>
                              changeRole.mutate({ userId: m.user_id, role: e.target.value as Role })
                            }
                          >
                            <option value="admin">Administrator</option>
                            <option value="operator">Operator</option>
                            <option value="viewer">Viewer</option>
                          </select>
                        ) : (
                          humanize(m.role)
                        )}
                      </td>
                      <td>
                        <Time iso={m.joined_at} />
                      </td>
                      <td>
                        {manage && (
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={remove.isPending}
                            onClick={() => {
                              if (window.confirm(`Remove ${m.email} from this workspace?`))
                                remove.mutate(m.user_id);
                            }}
                          >
                            Remove
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </QueryState>
      </Card>
      <MutationError error={changeRole.error ?? remove.error} />
      <Card title="What each role can do">
        <dl className="kv">
          <dt>Viewer</dt>
          <dd>Read integrations, events, investigations and the audit log.</dd>
          <dt>Operator</dt>
          <dd>
            Everything a viewer can, plus pause/resume integrations, replay events, run investigations and
            record decisions.
          </dd>
          <dt>Administrator</dt>
          <dd>Everything an operator can, plus manage connections, integration configuration and members.</dd>
        </dl>
        <p className="field-hint" style={{ marginTop: 8 }}>
          Permissions are enforced by the API on every request; this screen only reflects them.
        </p>
      </Card>
      {manage ? (
        <Card title="Add a member">
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              add.mutate(
                { email, display_name: "", role, password: password || null },
                {
                  onSuccess: () => {
                    setEmail("");
                    setPassword("");
                  },
                },
              );
            }}
          >
            <div className="grid-3">
              <Field label="Email">
                {(p) => (
                  <input
                    {...p}
                    type="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                  />
                )}
              </Field>
              <Field label="Role">
                {(p) => (
                  <select {...p} value={role} onChange={(e) => setRole(e.target.value as Role)}>
                    <option value="viewer">Viewer</option>
                    <option value="operator">Operator</option>
                    <option value="admin">Administrator</option>
                  </select>
                )}
              </Field>
              <Field
                label="Initial password"
                hint="Needed for a new account (12+ characters). Leave empty for an existing user."
              >
                {(p) => (
                  <input
                    {...p}
                    type="password"
                    autoComplete="new-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                )}
              </Field>
            </div>
            <MutationError error={add.error} />
            <div>
              <button className="btn btn-primary" type="submit" disabled={!email || add.isPending}>
                {add.isPending ? "Adding…" : "Add member"}
              </button>
            </div>
          </form>
        </Card>
      ) : (
        <Notice>Only administrators can add members or change roles.</Notice>
      )}
    </>
  );
}

function outcomeText(outcome: SimulatorOutcome): string {
  if (outcome.body.error) return `${outcome.body.error.code}: ${outcome.body.error.message}`;
  return (outcome.body.results ?? []).map((r) => humanize(r.outcome)).join(", ") || "No result";
}

function DemoModePicker({ data, allowed }: { data: DemoSetting; allowed: boolean }) {
  const setMode = useSetDemoMode();
  // The click is reflected immediately from local state, then confirmed (or
  // reverted, with an error) when the server answers.
  const [choice, setChoice] = useState<string | null>(null);
  const selected = choice ?? data.mode;
  return (
    <div className="stack">
      {!data.token_configured && (
        <Notice tone="warning">
          The demo destination’s token (<code>RELAY_SECRET_DEMO_DESTINATION_TOKEN</code>) is not set on the
          server, so deliveries to it will fail.
        </Notice>
      )}
      <fieldset className="stack-sm" style={{ border: 0, padding: 0, margin: 0 }} disabled={!allowed}>
        <legend className="field-label">Behaviour</legend>
        {Object.entries(data.modes).map(([mode, description]) => (
          <label key={mode} className="row" style={{ gap: 8, alignItems: "flex-start", flexWrap: "nowrap" }}>
            <input
              type="radio"
              name="demo-mode"
              checked={selected === mode}
              onChange={() => {
                setChoice(mode);
                setMode.mutate(
                  { mode, fail_first_n: data.fail_first_n, delay_seconds: data.delay_seconds },
                  { onSettled: () => setChoice(null) },
                );
              }}
            />
            <span>
              <strong>{humanize(mode)}</strong>
              <span className="secondary"> — {description}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <span className="field-hint" aria-live="polite">
        {setMode.isPending ? "Saving…" : `Current behaviour: ${humanize(data.mode)}.`}
      </span>
      <MutationError error={setMode.error} />
    </div>
  );
}

function DemoTools() {
  const { can } = useAuth();
  const allowed = can("demo:use");
  const connections = useConnections();
  const scenarios = useScenarios(true);
  const setting = useDemoSetting(true);
  const received = useDemoReceived(true);
  const simulate = useSimulate();
  const sources = (connections.data ?? []).filter((c) => c.direction === "source");
  const [connectionId, setConnectionId] = useState("");
  const [scenario, setScenario] = useState("valid_order");
  const [count, setCount] = useState(1);
  const sourceId = connectionId || sources[0]?.id || "";

  return (
    <>
      <Notice>
        <strong>Demo tooling.</strong> These controls exist so failure handling can be reproduced without an
        external system. They are not part of the product path.
      </Notice>
      {!allowed && (
        <Notice>Your role can view demo state but not send events or change the demo destination.</Notice>
      )}
      <div className="grid-2">
        <Card title="1 · Send events from the simulated source">
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              if (sourceId) simulate.mutate({ connection_id: sourceId, scenario, count });
            }}
          >
            <Field label="Source connection">
              {(p) => (
                <select {...p} value={sourceId} onChange={(e) => setConnectionId(e.target.value)}>
                  {sources.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            <Field label="Scenario" hint={scenarios.data?.scenarios[scenario]}>
              {(p) => (
                <select {...p} value={scenario} onChange={(e) => setScenario(e.target.value)}>
                  {Object.keys(scenarios.data?.scenarios ?? { valid_order: "" }).map((key) => (
                    <option key={key} value={key}>
                      {humanize(key)}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            <Field label="How many">
              {(p) => (
                <input
                  {...p}
                  type="number"
                  min={1}
                  max={20}
                  value={count}
                  onChange={(e) => setCount(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
                />
              )}
            </Field>
            <div>
              <button
                className="btn btn-primary"
                type="submit"
                disabled={!allowed || !sourceId || simulate.isPending}
              >
                {simulate.isPending ? "Sending…" : "Send"}
              </button>
            </div>
            <MutationError error={simulate.error} />
            {simulate.data && (
              <div className="table-wrap" aria-live="polite">
                <table>
                  <caption className="sr-only">What the sender received</caption>
                  <thead>
                    <tr>
                      <th scope="col">HTTP</th>
                      <th scope="col">Result</th>
                      <th scope="col">Event</th>
                    </tr>
                  </thead>
                  <tbody>
                    {simulate.data.map((o, i) => {
                      const eventId = o.body.results?.[0]?.event_id;
                      return (
                        <tr key={i}>
                          <td>{o.http_status}</td>
                          <td>{outcomeText(o)}</td>
                          <td>
                            {eventId ? (
                              <Link to={`/events/${eventId}`}>Open</Link>
                            ) : (
                              <span className="muted">Not stored</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </form>
        </Card>

        <Card title="2 · Choose how the demo destination behaves">
          <QueryState query={setting}>
            {(data) => <DemoModePicker data={data} allowed={allowed} />}
          </QueryState>
        </Card>
      </div>

      <Card title="3 · What the demo destination has received" flush>
        <QueryState
          query={received}
          isEmpty={(rows) => rows.length === 0}
          empty={<EmptyState title="Nothing received yet" />}
        >
          {(rows) => (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Idempotency key (event ID)</th>
                    <th scope="col" className="num">
                      Requests seen
                    </th>
                    <th scope="col">Stored</th>
                    <th scope="col">First seen</th>
                    <th scope="col">Order</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.idempotency_key}>
                      <td>
                        <Link to={`/events/${r.idempotency_key}`} className="mono">
                          {r.idempotency_key.slice(0, 8)}
                        </Link>
                      </td>
                      <td className="num">{r.attempts_seen}</td>
                      <td>
                        {r.accepted ? (
                          <Badge tone="good" icon="✓">
                            Once
                          </Badge>
                        ) : (
                          <Badge>Not stored</Badge>
                        )}
                      </td>
                      <td>
                        <Time iso={r.first_seen_at} relative />
                      </td>
                      <td>
                        {r.payload ? (
                          <details>
                            <summary>Show</summary>
                            <JsonView value={r.payload} label="Stored order" />
                          </details>
                        ) : (
                          <span className="muted">—</span>
                        )}
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

export function SettingsPage() {
  const [params, setParams] = useSearchParams();
  const tab = (
    ["connections", "members", "demo"].includes(params.get("tab") ?? "") ? params.get("tab") : "connections"
  ) as SettingsTab;
  return (
    <>
      <PageHeader title="Settings" description="Connections, workspace members, and demo tooling." />
      <Tabs
        label="Settings sections"
        value={tab}
        onChange={(next) => setParams({ tab: next }, { replace: true })}
        tabs={[
          { id: "connections", label: "Connections" },
          { id: "members", label: "Members" },
          { id: "demo", label: "Demo tools" },
        ]}
      />
      {tab === "connections" && <Connections />}
      {tab === "members" && <Members />}
      {tab === "demo" && <DemoTools />}
    </>
  );
}
