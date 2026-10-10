import { useMemo, useState } from "react";
import { usePreview, useValidateIntegration, type IntegrationInput } from "../api/hooks";
import type { Connection, FilterRule, IntegrationConfig, IntegrationDetail, Mapping } from "../api/types";
import { parseJson } from "../lib/format";
import { Card, Field, IssueList, JsonView, MutationError, Notice, Tabs } from "./ui";

export const EMPTY_CONFIG: IntegrationConfig = {
  event_types: [],
  input_schema: null,
  output_schema: null,
  filters: [],
  passthrough: false,
  mappings: [{ target: "", source: "" }],
  redact_fields: [],
  retry: { max_attempts: 5, base_delay_seconds: 2, max_delay_seconds: 300 },
  delivery: { timeout_seconds: 10 },
};

const CONVERSIONS = [
  "string",
  "integer",
  "number",
  "boolean",
  "datetime",
  "datetime_from_epoch_seconds",
  "datetime_from_epoch_millis",
  "date",
  "uppercase",
  "lowercase",
  "trim",
];
const FILTER_OPS = ["eq", "ne", "in", "not_in", "exists", "not_exists", "gt", "gte", "lt", "lte", "contains"];
const SAMPLE_PAYLOAD = `{
  "order_no": "A-1001",
  "status": "paid",
  "customer": { "name": "Ada Lovelace", "email": "ada@example.com" },
  "items": [{ "sku": "kb-100", "qty": "2", "unit_cost": "49.90" }],
  "currency": "usd",
  "placed_at": "2026-03-01T10:15:00Z"
}`;

/** "paid" -> "paid", 3 -> 3, true -> true: lets a text box hold any JSON scalar. */
function parseScalar(text: string): unknown {
  const result = parseJson(text);
  return result.ok ? result.value : text;
}
function showScalar(value: unknown): string {
  if (value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function MappingRows({ mappings, onChange }: { mappings: Mapping[]; onChange: (next: Mapping[]) => void }) {
  const set = (index: number, patch: Partial<Mapping> | ((m: Mapping) => Mapping)) =>
    onChange(
      mappings.map((m, i) =>
        i === index ? (typeof patch === "function" ? patch(m) : { ...m, ...patch }) : m,
      ),
    );
  return (
    <div className="table-wrap">
      <table className="mapping-table">
        <thead>
          <tr>
            <th scope="col">Output field</th>
            <th scope="col">From</th>
            <th scope="col">Source path or constant</th>
            <th scope="col">Convert</th>
            <th scope="col">Required</th>
            <th scope="col">Default</th>
            <th scope="col">
              <span className="sr-only">Remove</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {mappings.map((m, index) => {
            const isConst = "const" in m && m.source === undefined;
            const n = index + 1;
            return (
              <tr key={index}>
                <td>
                  <input
                    type="text"
                    aria-label={`Mapping ${n} output field`}
                    value={m.target}
                    onChange={(e) => set(index, { target: e.target.value })}
                    placeholder="customer.name"
                  />
                </td>
                <td>
                  <select
                    aria-label={`Mapping ${n} kind`}
                    value={isConst ? "const" : "source"}
                    onChange={(e) =>
                      set(index, (old) =>
                        e.target.value === "const"
                          ? { target: old.target, const: "" }
                          : { target: old.target, source: "" },
                      )
                    }
                  >
                    <option value="source">Payload field</option>
                    <option value="const">Constant</option>
                  </select>
                </td>
                <td>
                  {isConst ? (
                    <input
                      type="text"
                      aria-label={`Mapping ${n} constant`}
                      value={showScalar(m.const)}
                      onChange={(e) => set(index, { const: parseScalar(e.target.value) as Mapping["const"] })}
                    />
                  ) : (
                    <>
                      <input
                        type="text"
                        aria-label={`Mapping ${n} source path`}
                        value={m.source ?? ""}
                        onChange={(e) => set(index, { source: e.target.value })}
                        placeholder="customer.name or items[0].sku"
                      />
                      {m.each && (
                        <div className="field-hint">
                          Applies {m.each.length} nested mapping(s) to each array item — edit in the JSON tab.
                        </div>
                      )}
                    </>
                  )}
                </td>
                <td>
                  <select
                    aria-label={`Mapping ${n} conversion`}
                    disabled={isConst || Boolean(m.each)}
                    value={m.convert ?? ""}
                    onChange={(e) =>
                      set(index, (old) => {
                        const { convert: _drop, ...rest } = old;
                        return e.target.value ? { ...rest, convert: e.target.value } : rest;
                      })
                    }
                  >
                    <option value="">None</option>
                    {CONVERSIONS.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Mapping ${n} required`}
                    disabled={isConst}
                    checked={Boolean(m.required)}
                    onChange={(e) =>
                      set(index, (old) => {
                        const { required: _r, default: _d, ...rest } = old;
                        return e.target.checked ? { ...rest, required: true } : rest;
                      })
                    }
                  />
                </td>
                <td>
                  <input
                    type="text"
                    aria-label={`Mapping ${n} default`}
                    disabled={isConst || Boolean(m.required) || Boolean(m.each)}
                    value={showScalar(m.default)}
                    placeholder="none"
                    onChange={(e) =>
                      set(index, (old) => {
                        const { default: _d, ...rest } = old;
                        return e.target.value === ""
                          ? rest
                          : { ...rest, default: parseScalar(e.target.value) as Mapping["default"] };
                      })
                    }
                  />
                </td>
                <td>
                  <button
                    type="button"
                    className="btn btn-sm"
                    aria-label={`Remove mapping ${n}`}
                    onClick={() => onChange(mappings.filter((_, i) => i !== index))}
                  >
                    Remove
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="card-body">
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => onChange([...mappings, { target: "", source: "" }])}
        >
          Add mapping
        </button>
      </div>
    </div>
  );
}

function FilterRows({
  filters,
  onChange,
}: {
  filters: FilterRule[];
  onChange: (next: FilterRule[]) => void;
}) {
  const set = (index: number, next: FilterRule) => onChange(filters.map((f, i) => (i === index ? next : f)));
  return (
    <div className="stack-sm">
      {filters.length === 0 && <p className="muted">No filters: every accepted event is processed.</p>}
      {filters.map((f, index) => {
        const unary = f.op === "exists" || f.op === "not_exists";
        return (
          <div className="row" key={index}>
            <input
              type="text"
              aria-label={`Filter ${index + 1} path`}
              value={f.path}
              placeholder="status"
              style={{ maxWidth: 220 }}
              onChange={(e) => set(index, { ...f, path: e.target.value })}
            />
            <select
              aria-label={`Filter ${index + 1} operator`}
              value={f.op}
              style={{ width: "auto" }}
              onChange={(e) => {
                const op = e.target.value;
                set(
                  index,
                  op === "exists" || op === "not_exists"
                    ? { path: f.path, op }
                    : { path: f.path, op, value: f.value ?? "" },
                );
              }}
            >
              {FILTER_OPS.map((op) => (
                <option key={op} value={op}>
                  {op}
                </option>
              ))}
            </select>
            {!unary && (
              <input
                type="text"
                aria-label={`Filter ${index + 1} value`}
                value={showScalar(f.value)}
                placeholder={f.op === "in" || f.op === "not_in" ? '["paid","authorized"]' : "paid"}
                style={{ maxWidth: 260 }}
                onChange={(e) => set(index, { ...f, value: parseScalar(e.target.value) })}
              />
            )}
            <button
              type="button"
              className="btn btn-sm"
              aria-label={`Remove filter ${index + 1}`}
              onClick={() => onChange(filters.filter((_, i) => i !== index))}
            >
              Remove
            </button>
          </div>
        );
      })}
      <div>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => onChange([...filters, { path: "", op: "eq", value: "" }])}
        >
          Add filter
        </button>
      </div>
    </div>
  );
}

function SchemaField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: Record<string, unknown> | null;
  onChange: (next: Record<string, unknown> | null) => void;
}) {
  const [text, setText] = useState(value ? JSON.stringify(value, null, 2) : "");
  const [error, setError] = useState<string | null>(null);
  return (
    <Field label={label} hint="JSON Schema (restricted subset). Leave empty for no check." error={error}>
      {(props) => (
        <textarea
          {...props}
          rows={10}
          spellCheck={false}
          value={text}
          onChange={(e) => {
            const next = e.target.value;
            setText(next);
            if (next.trim() === "") {
              setError(null);
              onChange(null);
              return;
            }
            const parsed = parseJson(next);
            if (!parsed.ok) setError(`Not valid JSON: ${parsed.error}`);
            else if (typeof parsed.value !== "object" || parsed.value === null || Array.isArray(parsed.value))
              setError("A schema must be a JSON object.");
            else {
              setError(null);
              onChange(parsed.value as Record<string, unknown>);
            }
          }}
        />
      )}
    </Field>
  );
}

function num(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

type EditorTab = "form" | "json" | "test";

export function IntegrationEditor({
  integration,
  connections,
  saving,
  saveError,
  onSave,
  onCancel,
}: {
  integration: IntegrationDetail | null;
  connections: Connection[];
  saving: boolean;
  saveError: unknown;
  onSave: (input: IntegrationInput) => void;
  onCancel?: () => void;
}) {
  const sources = connections.filter((c) => c.direction === "source");
  const destinations = connections.filter((c) => c.direction === "destination");
  const [name, setName] = useState(integration?.name ?? "");
  const [description, setDescription] = useState(integration?.description ?? "");
  const [sourceId, setSourceId] = useState(integration?.source_connection_id ?? sources[0]?.id ?? "");
  const [destinationId, setDestinationId] = useState(
    integration?.destination_connection_id ?? destinations[0]?.id ?? "",
  );
  const [config, setConfig] = useState<IntegrationConfig>(integration?.config ?? EMPTY_CONFIG);
  const [changeNote, setChangeNote] = useState("");
  const [tab, setTab] = useState<EditorTab>("form");
  const [jsonText, setJsonText] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [sample, setSample] = useState(SAMPLE_PAYLOAD);
  const validate = useValidateIntegration(integration?.id ?? "");
  const preview = usePreview();

  const patch = (next: Partial<IntegrationConfig>) => setConfig((old) => ({ ...old, ...next }));
  const dirty = useMemo(
    () =>
      !integration ||
      name !== integration.name ||
      description !== integration.description ||
      sourceId !== integration.source_connection_id ||
      destinationId !== integration.destination_connection_id ||
      JSON.stringify(config) !== JSON.stringify(integration.config),
    [integration, name, description, sourceId, destinationId, config],
  );
  const samplePayload = parseJson(sample);
  const canSave = Boolean(name.trim() && sourceId && destinationId) && dirty && !jsonError;

  function openTab(next: EditorTab) {
    if (next === "json") {
      setJsonText(JSON.stringify(config, null, 2));
      setJsonError(null);
    }
    setTab(next);
  }

  return (
    <div className="stack">
      <Card title="Basics">
        <div className="grid-2">
          <Field label="Name">
            {(p) => (
              <input
                {...p}
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={200}
              />
            )}
          </Field>
          <Field label="Description">
            {(p) => (
              <input
                {...p}
                type="text"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                maxLength={2000}
              />
            )}
          </Field>
          <Field
            label="Source connection"
            hint={sources.length === 0 ? "Create a source connection in Settings first." : undefined}
          >
            {(p) => (
              <select {...p} value={sourceId} onChange={(e) => setSourceId(e.target.value)}>
                {sources.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} ({c.adapter_type})
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field
            label="Destination connection"
            hint={
              destinations.length === 0 ? "Create a destination connection in Settings first." : undefined
            }
          >
            {(p) => (
              <select {...p} value={destinationId} onChange={(e) => setDestinationId(e.target.value)}>
                {destinations.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} ({c.adapter_type})
                  </option>
                ))}
              </select>
            )}
          </Field>
        </div>
      </Card>

      <Tabs
        label="Configuration view"
        value={tab}
        onChange={openTab}
        tabs={[
          { id: "form", label: "Form" },
          { id: "json", label: "JSON" },
          { id: "test", label: "Test with a sample" },
        ]}
      />

      {tab === "form" && (
        <>
          <Card title="Routing and filters">
            <div className="stack">
              <Field
                label="Event types"
                hint="Comma-separated. Leave empty to accept every event sent to the source connection."
              >
                {(p) => (
                  <input
                    {...p}
                    type="text"
                    value={config.event_types.join(", ")}
                    placeholder="order.created, order.updated"
                    onChange={(e) =>
                      patch({
                        event_types: e.target.value
                          .split(",")
                          .map((s) => s.trim())
                          .filter(Boolean),
                      })
                    }
                  />
                )}
              </Field>
              <div>
                <div className="field-label">Filters (an event is processed only if every rule matches)</div>
                <FilterRows filters={config.filters} onChange={(filters) => patch({ filters })} />
              </div>
            </div>
          </Card>
          <Card
            title="Mappings"
            flush
            actions={
              <label className="row" style={{ gap: 6 }}>
                <input
                  type="checkbox"
                  checked={config.passthrough}
                  onChange={(e) =>
                    patch({
                      passthrough: e.target.checked,
                      mappings: e.target.checked
                        ? []
                        : config.mappings.length
                          ? config.mappings
                          : [{ target: "", source: "" }],
                    })
                  }
                />
                Pass the payload through unchanged
              </label>
            }
          >
            {config.passthrough ? (
              <div className="card-body muted">
                The payload is delivered as received. Schemas and filters still apply.
              </div>
            ) : (
              <MappingRows mappings={config.mappings} onChange={(mappings) => patch({ mappings })} />
            )}
          </Card>
          <Card title="Schemas">
            <div className="grid-2">
              <SchemaField
                key={`in-${integration?.current_version_id ?? "new"}`}
                label="Input schema (what the source must send)"
                value={config.input_schema}
                onChange={(input_schema) => patch({ input_schema })}
              />
              <SchemaField
                key={`out-${integration?.current_version_id ?? "new"}`}
                label="Output schema (what the destination must receive)"
                value={config.output_schema}
                onChange={(output_schema) => patch({ output_schema })}
              />
            </div>
          </Card>
          <Card title="Reliability">
            <div className="grid-2">
              <Field label="Maximum delivery attempts" hint="1 to 10. Exhausted events are dead-lettered.">
                {(p) => (
                  <input
                    {...p}
                    type="number"
                    min={1}
                    max={10}
                    value={config.retry.max_attempts}
                    onChange={(e) =>
                      patch({ retry: { ...config.retry, max_attempts: num(e.target.value, 5) } })
                    }
                  />
                )}
              </Field>
              <Field label="Delivery timeout (seconds)" hint="0.5 to 30. A hard deadline per attempt.">
                {(p) => (
                  <input
                    {...p}
                    type="number"
                    min={0.5}
                    max={30}
                    step={0.5}
                    value={config.delivery.timeout_seconds}
                    onChange={(e) => patch({ delivery: { timeout_seconds: num(e.target.value, 10) } })}
                  />
                )}
              </Field>
              <Field label="First retry delay (seconds)" hint="Doubles on each retry, with random jitter.">
                {(p) => (
                  <input
                    {...p}
                    type="number"
                    min={0.1}
                    max={3600}
                    step={0.1}
                    value={config.retry.base_delay_seconds}
                    onChange={(e) =>
                      patch({ retry: { ...config.retry, base_delay_seconds: num(e.target.value, 2) } })
                    }
                  />
                )}
              </Field>
              <Field label="Maximum retry delay (seconds)" hint="Also caps a destination’s Retry-After.">
                {(p) => (
                  <input
                    {...p}
                    type="number"
                    min={1}
                    max={86400}
                    value={config.retry.max_delay_seconds}
                    onChange={(e) =>
                      patch({ retry: { ...config.retry, max_delay_seconds: num(e.target.value, 300) } })
                    }
                  />
                )}
              </Field>
              <Field
                label="Extra fields to redact"
                hint="Comma-separated key names hidden in the UI, on top of the built-in list."
              >
                {(p) => (
                  <input
                    {...p}
                    type="text"
                    value={config.redact_fields.join(", ")}
                    onChange={(e) =>
                      patch({
                        redact_fields: e.target.value
                          .split(",")
                          .map((s) => s.trim())
                          .filter(Boolean),
                      })
                    }
                  />
                )}
              </Field>
            </div>
          </Card>
        </>
      )}

      {tab === "json" && (
        <Card title="Configuration as JSON">
          <Field
            label="Full configuration"
            hint="Edits here update the form. Needed for nested “each” mappings."
            error={jsonError}
          >
            {(p) => (
              <textarea
                {...p}
                rows={26}
                spellCheck={false}
                value={jsonText}
                onChange={(e) => {
                  setJsonText(e.target.value);
                  const parsed = parseJson(e.target.value);
                  if (!parsed.ok) setJsonError(`Not valid JSON: ${parsed.error}`);
                  else if (
                    typeof parsed.value !== "object" ||
                    parsed.value === null ||
                    Array.isArray(parsed.value)
                  )
                    setJsonError("Configuration must be a JSON object.");
                  else {
                    setJsonError(null);
                    setConfig({ ...EMPTY_CONFIG, ...(parsed.value as Partial<IntegrationConfig>) });
                  }
                }}
              />
            )}
          </Field>
        </Card>
      )}

      {tab === "test" && (
        <Card title="Test this configuration against a sample payload">
          <div className="grid-2">
            <div className="stack-sm">
              <Field
                label="Sample payload"
                error={samplePayload.ok ? null : `Not valid JSON: ${samplePayload.error}`}
              >
                {(p) => (
                  <textarea
                    {...p}
                    rows={14}
                    spellCheck={false}
                    value={sample}
                    onChange={(e) => setSample(e.target.value)}
                  />
                )}
              </Field>
              <div>
                <button
                  type="button"
                  className="btn"
                  disabled={!samplePayload.ok || preview.isPending}
                  onClick={() => samplePayload.ok && preview.mutate({ config, payload: samplePayload.value })}
                >
                  {preview.isPending ? "Running…" : "Run test"}
                </button>
              </div>
              <span className="field-hint">A dry run: nothing is stored and nothing is delivered.</span>
            </div>
            <div className="stack-sm" aria-live="polite">
              <MutationError error={preview.error} />
              {preview.data?.kind === "ready" && (
                <>
                  <Notice tone="good">This payload would be delivered.</Notice>
                  <JsonView value={preview.data.output} label="Payload the destination would receive" />
                </>
              )}
              {preview.data?.kind === "filtered" && (
                <Notice>This payload would be skipped: {preview.data.filter_reason}.</Notice>
              )}
              {(preview.data?.kind === "validation_failed" || preview.data?.kind === "invalid_config") && (
                <Notice tone="critical">
                  <strong>
                    {preview.data.kind === "invalid_config"
                      ? "The configuration is not valid."
                      : `Failed at stage “${preview.data.stage}”.`}
                  </strong>
                  <IssueList issues={preview.data.issues} />
                </Notice>
              )}
              {!preview.data && !preview.error && <p className="muted">Results appear here.</p>}
            </div>
          </div>
        </Card>
      )}

      <Card title="Check and save">
        <div className="stack">
          {integration && (
            <div className="row">
              <button
                type="button"
                className="btn"
                disabled={validate.isPending || Boolean(jsonError)}
                onClick={() => validate.mutate(config)}
              >
                {validate.isPending ? "Checking…" : "Validate"}
              </button>
              <span className="field-hint">
                Checks this draft, the connections and their secret references without saving.
              </span>
            </div>
          )}
          <div aria-live="polite">
            <MutationError error={validate.error} />
            {validate.data &&
              (validate.data.valid ? (
                <Notice tone="good">
                  No blocking problems.
                  <IssueList issues={validate.data.issues} />
                </Notice>
              ) : (
                <Notice tone="critical">
                  <strong>This configuration cannot be activated yet.</strong>
                  <IssueList issues={validate.data.issues} />
                </Notice>
              ))}
          </div>
          {integration && (
            <Field label="Change note" hint="Saved with the new version so the history explains itself.">
              {(p) => (
                <input
                  {...p}
                  type="text"
                  value={changeNote}
                  maxLength={500}
                  onChange={(e) => setChangeNote(e.target.value)}
                  placeholder="What changed and why"
                />
              )}
            </Field>
          )}
          <MutationError error={saveError} />
          <div className="row">
            <button
              type="button"
              className="btn btn-primary"
              disabled={!canSave || saving}
              onClick={() =>
                onSave({
                  name: name.trim(),
                  description,
                  source_connection_id: sourceId,
                  destination_connection_id: destinationId,
                  config,
                  change_note: changeNote,
                })
              }
            >
              {saving ? "Saving…" : integration ? "Save as new version" : "Create draft integration"}
            </button>
            {onCancel && (
              <button type="button" className="btn" onClick={onCancel}>
                Cancel
              </button>
            )}
            {integration && !dirty && <span className="field-hint">No unsaved changes.</span>}
          </div>
        </div>
      </Card>
    </div>
  );
}
