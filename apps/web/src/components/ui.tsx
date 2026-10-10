import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";
import type { EventStatus, IntegrationStatus, Issue } from "../api/types";
import { formatDateTime, formatRelative, humanize } from "../lib/format";

type Tone = "good" | "warning" | "serious" | "critical" | "info" | "neutral";

// Each status has an icon and a label as well as a tone, so meaning never
// depends on colour alone.
const EVENT_STATUS: Record<EventStatus, { label: string; tone: Tone; icon: string; hint: string }> = {
  RECEIVED: { label: "Received", tone: "info", icon: "●", hint: "Stored; not yet routed." },
  PENDING_PUBLISH: {
    label: "Pending publish",
    tone: "info",
    icon: "●",
    hint: "Waiting to be handed to the queue.",
  },
  QUEUED: { label: "Queued", tone: "info", icon: "●", hint: "Accepted by the queue; waiting for a worker." },
  PROCESSING: { label: "Processing", tone: "info", icon: "◐", hint: "A worker is delivering it now." },
  RETRY_SCHEDULED: {
    label: "Retry scheduled",
    tone: "warning",
    icon: "↻",
    hint: "A delivery failed; another attempt is scheduled.",
  },
  HELD: { label: "Held", tone: "serious", icon: "❙❙", hint: "The integration is paused." },
  REPLAY_REQUESTED: {
    label: "Replay requested",
    tone: "info",
    icon: "↻",
    hint: "An operator asked for it to be replayed.",
  },
  DELIVERED: { label: "Delivered", tone: "good", icon: "✓", hint: "The destination accepted it." },
  FILTERED: {
    label: "Filtered",
    tone: "neutral",
    icon: "–",
    hint: "Skipped by the integration's filter rules.",
  },
  VALIDATION_FAILED: {
    label: "Validation failed",
    tone: "critical",
    icon: "✕",
    hint: "Rejected by a schema or mapping. Not delivered.",
  },
  DEAD_LETTERED: {
    label: "Dead-lettered",
    tone: "critical",
    icon: "✕",
    hint: "Delivery failed permanently or ran out of attempts.",
  },
};

const INTEGRATION_STATUS: Record<IntegrationStatus, { label: string; tone: Tone; icon: string }> = {
  draft: { label: "Draft", tone: "neutral", icon: "○" },
  active: { label: "Active", tone: "good", icon: "✓" },
  paused: { label: "Paused", tone: "serious", icon: "❙❙" },
  disabled: { label: "Disabled", tone: "neutral", icon: "–" },
};

export function Badge({
  tone = "neutral",
  icon,
  children,
  title,
}: {
  tone?: Tone;
  icon?: string;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`badge tone-${tone}`} title={title}>
      {icon && (
        <span className="badge-icon" aria-hidden="true">
          {icon}
        </span>
      )}
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: EventStatus }) {
  const meta = EVENT_STATUS[status] ?? {
    label: humanize(status),
    tone: "neutral" as Tone,
    icon: "?",
    hint: "",
  };
  return (
    <Badge tone={meta.tone} icon={meta.icon} title={meta.hint}>
      {meta.label}
    </Badge>
  );
}

export function eventStatusLabel(status: EventStatus): string {
  return EVENT_STATUS[status]?.label ?? humanize(status);
}

export function IntegrationStatusBadge({ status }: { status: IntegrationStatus }) {
  const meta = INTEGRATION_STATUS[status];
  return (
    <Badge tone={meta.tone} icon={meta.icon}>
      {meta.label}
    </Badge>
  );
}

export function PageHeader({
  title,
  description,
  breadcrumb,
  actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  breadcrumb?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        {breadcrumb && <div className="breadcrumb">{breadcrumb}</div>}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="actions">{actions}</div>}
    </header>
  );
}

export function Card({
  title,
  actions,
  children,
  flush = false,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className="card">
      {(title || actions) && (
        <div className="card-header">
          {title ? <h2>{title}</h2> : <span />}
          {actions && <div className="actions">{actions}</div>}
        </div>
      )}
      <div className={flush ? undefined : "card-body"}>{children}</div>
    </section>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state">
      <strong>{title}</strong>
      {children && <div>{children}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const apiError = error instanceof ApiError ? error : null;
  if (apiError?.isForbidden) {
    return (
      <div className="state" role="alert">
        <strong>You don’t have access to this</strong>
        <div>{apiError.message}. Ask a workspace administrator if you need it.</div>
      </div>
    );
  }
  return (
    <div className="state" role="alert">
      <strong>{apiError?.status === 404 ? "Not found" : "Something went wrong"}</strong>
      <div>{apiError?.message ?? "An unexpected error occurred."}</div>
      {apiError?.correlationId && (
        <div className="muted">
          Reference <code>{apiError.correlationId}</code>
        </div>
      )}
      {onRetry && apiError?.status !== 404 && (
        <button type="button" className="btn" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

interface QueryLike<T> {
  data: T | undefined;
  error: unknown;
  isPending: boolean;
  isError: boolean;
  refetch: () => unknown;
}

/** One consistent loading / error / empty / ready switch for every data view. */
export function QueryState<T>({
  query,
  isEmpty,
  empty,
  children,
}: {
  query: QueryLike<T>;
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (query.isPending) return <Loading />;
  if (query.isError || query.data === undefined)
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (isEmpty?.(query.data)) return <>{empty ?? <EmptyState title="Nothing here yet" />}</>;
  return <>{children(query.data)}</>;
}

export function Notice({
  tone = "info",
  children,
}: {
  tone?: "info" | "warning" | "critical" | "good";
  children: ReactNode;
}) {
  const icon = { info: "ℹ", warning: "⚠", critical: "✕", good: "✓" }[tone];
  return (
    <div className={`notice notice-${tone}`} role={tone === "critical" ? "alert" : "status"}>
      <span aria-hidden="true">{icon}</span>
      <div style={{ minWidth: 0 }}>{children}</div>
    </div>
  );
}

export function MutationError({ error }: { error: unknown }) {
  if (!error) return null;
  const apiError = error instanceof ApiError ? error : null;
  const issues = (apiError?.details ?? []).filter((d) => typeof d.message === "string") as unknown as Issue[];
  return (
    <Notice tone="critical">
      <strong>{apiError?.message ?? "The request failed."}</strong>
      {issues.length > 0 && <IssueList issues={issues} />}
      {apiError?.retryAfterSeconds && <div>Try again in {apiError.retryAfterSeconds}s.</div>}
      {apiError?.correlationId && (
        <div className="muted">
          Reference <code>{apiError.correlationId}</code>
        </div>
      )}
    </Notice>
  );
}

export function IssueList({ issues }: { issues: Issue[] }) {
  if (issues.length === 0) return null;
  return (
    <ul className="issue-list" style={{ marginTop: 6 }}>
      {issues.map((issue, index) => (
        <li key={`${issue.path}-${issue.code}-${index}`}>
          <Badge
            tone={issue.severity === "warning" ? "warning" : "critical"}
            icon={issue.severity === "warning" ? "⚠" : "✕"}
          >
            {issue.severity === "warning" ? "Warning" : "Error"}
          </Badge>
          <span>
            {issue.path && <code>{issue.path}</code>} {issue.message}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function Time({ iso, relative = false }: { iso: string | null | undefined; relative?: boolean }) {
  if (!iso) return <span className="muted">—</span>;
  return (
    <time dateTime={iso} title={formatDateTime(iso)} className="nowrap">
      {relative ? formatRelative(iso) : formatDateTime(iso)}
    </time>
  );
}

export function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-sm"
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      <span aria-live="polite">{copied ? "Copied" : label}</span>
    </button>
  );
}

export function JsonView({ value, label }: { value: unknown; label: string }) {
  const text = value === null || value === undefined ? "" : JSON.stringify(value, null, 2);
  if (!text) return <p className="muted">None.</p>;
  return (
    <div className="stack-sm">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="field-label">{label}</span>
        <CopyButton value={text} />
      </div>
      {/* Rendered as text content: payloads are untrusted and never interpreted as markup. */}
      <pre className="json" tabIndex={0} aria-label={label}>
        {text}
      </pre>
    </div>
  );
}

export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: boolean }) => ReactNode;
}) {
  const id = useId();
  const describedBy =
    [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {hint && (
        <span id={`${id}-hint`} className="field-hint">
          {hint}
        </span>
      )}
      {error && (
        <span id={`${id}-error`} className="field-error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}

/** Modal built on the native <dialog>, which provides focus trapping and Escape handling. */
export function Dialog({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    if (open && !node.open) node.showModal?.();
    if (!open && node.open) node.close();
  }, [open]);
  if (!open) return null;
  return (
    <dialog ref={ref} aria-labelledby={titleId} onClose={onClose} onCancel={onClose}>
      <div className="card-header">
        <h2 id={titleId}>{title}</h2>
      </div>
      <div className="dialog-body">{children}</div>
      <div className="dialog-footer">{footer}</div>
    </dialog>
  );
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
  label: string;
}) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          aria-selected={value === tab.id}
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

export function LoadMore({
  hasMore,
  loading,
  onClick,
}: {
  hasMore: boolean;
  loading: boolean;
  onClick: () => void;
}) {
  if (!hasMore) return null;
  return (
    <div className="card-body" style={{ textAlign: "center" }}>
      <button type="button" className="btn" onClick={onClick} disabled={loading}>
        {loading ? "Loading…" : "Load more"}
      </button>
    </div>
  );
}
