// Shapes returned by the Relay API. Kept in step with docs/design/openapi.json.

export type Role = "admin" | "operator" | "viewer";

export type EventStatus =
  | "RECEIVED"
  | "HELD"
  | "VALIDATION_FAILED"
  | "FILTERED"
  | "PENDING_PUBLISH"
  | "QUEUED"
  | "PROCESSING"
  | "RETRY_SCHEDULED"
  | "DELIVERED"
  | "DEAD_LETTERED"
  | "REPLAY_REQUESTED";

export type IntegrationStatus = "draft" | "active" | "paused" | "disabled";

export interface Issue {
  code: string;
  path: string;
  message: string;
  severity: "error" | "warning";
}

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  is_demo: boolean;
  role: Role;
}

export interface Session {
  user: { id: string; email: string; display_name: string };
  workspaces: Workspace[];
  active_workspace_id: string | null;
  role: Role | null;
  permissions: string[];
  csrf_token: string;
  expires_at: string;
}

export interface Member {
  user_id: string;
  email: string;
  display_name: string;
  role: Role;
  is_active: boolean;
  joined_at: string;
}

export interface Connection {
  id: string;
  name: string;
  direction: "source" | "destination";
  adapter_type: string;
  config: Record<string, unknown>;
  secret_ref: string | null;
  secret_resolvable: boolean | null;
  is_active: boolean;
  webhook_path: string | null;
  warnings: Issue[];
  created_at: string;
  updated_at: string;
}

export interface Integration {
  id: string;
  name: string;
  description: string;
  status: IntegrationStatus;
  source_connection_id: string;
  source_connection_name: string | null;
  destination_connection_id: string;
  destination_connection_name: string | null;
  destination_adapter_type: string | null;
  current_version_id: string | null;
  current_version: number | null;
  created_at: string;
  updated_at: string;
}

export interface Mapping {
  target: string;
  source?: string;
  const?: string | number | boolean | null;
  default?: string | number | boolean | null;
  required?: boolean;
  convert?: string;
  each?: Mapping[];
}

export interface FilterRule {
  path: string;
  op: string;
  value?: unknown;
}

export interface IntegrationConfig {
  event_types: string[];
  input_schema: Record<string, unknown> | null;
  output_schema: Record<string, unknown> | null;
  filters: FilterRule[];
  passthrough: boolean;
  mappings: Mapping[];
  redact_fields: string[];
  retry: { max_attempts: number; base_delay_seconds: number; max_delay_seconds: number };
  delivery: { timeout_seconds: number };
}

export interface IntegrationDetail extends Integration {
  config: IntegrationConfig;
}

export interface IntegrationVersion {
  id: string;
  version: number;
  config: IntegrationConfig;
  change_note: string;
  created_by: string | null;
  created_at: string;
  is_current: boolean;
}

export interface ValidationResult {
  valid: boolean;
  issues: Issue[];
}

export interface PreviewResult {
  kind: "ready" | "validation_failed" | "filtered" | "invalid_config";
  stage: string | null;
  issues: Issue[];
  output: unknown;
  filter_reason: string | null;
}

export interface EventSummary {
  id: string;
  integration_id: string;
  integration_name: string | null;
  status: EventStatus;
  event_type: string | null;
  source_event_id: string | null;
  correlation_id: string;
  attempt_count: number;
  max_attempts: number;
  delivery_cycle: number;
  next_attempt_at: string | null;
  failure_stage: string | null;
  failure_category: string | null;
  last_error: string | null;
  received_at: string;
  updated_at: string;
  delivered_at: string | null;
  dead_lettered_at: string | null;
}

export interface Transition {
  id: number;
  from_status: string | null;
  to_status: EventStatus;
  reason: string;
  actor_type: string;
  actor_user_id: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
}

export interface Replay {
  id: string;
  event_id: string;
  cycle: number;
  version_mode: "original" | "current";
  integration_version_id: string;
  from_status: string;
  reason: string;
  requested_by: string | null;
  created_at: string;
}

export interface EventDetail extends EventSummary {
  integration_version_id: string;
  integration_version: number | null;
  payload: unknown;
  transformed_payload: unknown;
  payload_size_bytes: number;
  payload_purged: boolean;
  failure_details: Issue[] | null;
  replayable: boolean;
  transitions: Transition[];
  replays: Replay[];
}

export interface Attempt {
  id: string;
  cycle: number;
  attempt_number: number;
  status: "in_progress" | "succeeded" | "failed";
  disposition: string | null;
  failure_category: string | null;
  http_status: number | null;
  request_method: string | null;
  request_url: string | null;
  response_summary: string | null;
  retry_after_seconds: number | null;
  error_message: string | null;
  idempotency_key: string;
  integration_version_id: string;
  replay_operation_id: string | null;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface Fact {
  statement: string;
  evidence: string[];
}

export interface Recommendation {
  id: string;
  action: string;
  title: string;
  rationale: string;
  requires_approval: boolean;
  proposed_mappings: Mapping[] | null;
  validation: { valid: boolean; issues?: Issue[]; stage?: string | null } | null;
}

export interface InvestigationReport {
  summary: string;
  observed_facts: Fact[];
  likely_cause: {
    category: string;
    statement: string;
    confidence: "low" | "medium" | "high";
    evidence: string[];
  };
  uncertainty: string[];
  missing_evidence: string[];
  recommendations: Recommendation[];
}

export interface Decision {
  decision: "approved" | "rejected";
  note: string;
  decided_by: string | null;
  decided_at: string;
}

export interface Investigation {
  id: string;
  event_id: string;
  status: "completed" | "failed";
  mode: "ai" | "deterministic";
  provider: string | null;
  model: string | null;
  report: InvestigationReport | null;
  evidence_refs: string[];
  decisions: Record<string, Decision>;
  fallback_reason: string | null;
  error: string | null;
  duration_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  trace: { steps?: { tool?: string; ok?: boolean; step?: number }[]; enforcement_notes?: string[] } | null;
  requested_by: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface AuditEntry {
  id: string;
  action: string;
  entity_type: string;
  entity_id: string;
  actor_type: string;
  actor_user_id: string | null;
  actor_email: string | null;
  details: Record<string, unknown>;
  correlation_id: string | null;
  created_at: string;
}

export type DashboardWindow = "1h" | "24h" | "7d" | "30d";

export interface SeriesPoint {
  at: string;
  received: number;
  delivered: number;
  failed: number;
}

export interface Dashboard {
  window: DashboardWindow;
  since: string;
  generated_at: string;
  is_demo_workspace: boolean;
  events: {
    received: number;
    delivered: number;
    failed: number;
    in_flight: number;
    filtered: number;
    held: number;
    dead_lettered: number;
    validation_failed: number;
    by_status: Record<string, number>;
    success_rate: number | null;
  };
  attempts: {
    total: number;
    failed: number;
    retries: number;
    duration_ms_p50: number | null;
    duration_ms_p95: number | null;
  };
  live: {
    retry_scheduled: number;
    held: number;
    oldest_in_flight_age_seconds: number | null;
    oldest_pending_outbox_age_seconds: number | null;
  };
  recent_failures: EventSummary[];
  integrations: {
    id: string;
    name: string;
    status: IntegrationStatus;
    received: number;
    delivered: number;
    failed: number;
    in_flight: number;
    last_event_at: string | null;
    success_rate: number | null;
  }[];
  series: { bucket: "hour" | "day"; points: SeriesPoint[] };
  investigations: {
    total: number;
    ai: number;
    fell_back: number;
    failed: number;
    avg_duration_ms: number | null;
  };
}

export interface DemoSetting {
  mode: string;
  fail_first_n: number;
  delay_seconds: number;
  modes: Record<string, string>;
  token_configured: boolean;
}

export interface DemoReceived {
  idempotency_key: string;
  attempts_seen: number;
  accepted: boolean;
  payload: unknown;
  first_seen_at: string;
  accepted_at: string | null;
}

export interface SimulatorOutcome {
  scenario: string;
  source_event_id: string;
  http_status: number;
  body: {
    results?: { event_id: string; outcome: string; issues: Issue[] }[];
    error?: { code: string; message: string };
  };
}

export interface AiStatus {
  providers: { name: string; model: string | null; configured: boolean; kind: string }[];
  deterministic_fallback: boolean;
}
