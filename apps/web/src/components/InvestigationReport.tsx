import { useState } from "react";
import { Link } from "react-router-dom";
import { useDecision } from "../api/hooks";
import type { Investigation, Recommendation } from "../api/types";
import { useAuth } from "../auth";
import { formatDuration, humanize } from "../lib/format";
import { Badge, Card, IssueList, JsonView, MutationError, Notice, Time } from "./ui";

const CONFIDENCE_TONE = { low: "neutral", medium: "warning", high: "info" } as const;

function EvidenceChips({ refs }: { refs: string[] }) {
  if (refs.length === 0) return null;
  return (
    <span className="row" style={{ gap: 4 }}>
      {refs.map((ref) => (
        <span key={ref} className="chip" title="Reference to a stored record this statement is based on">
          {ref.length > 30 ? `${ref.slice(0, 22)}…${ref.slice(-4)}` : ref}
        </span>
      ))}
    </span>
  );
}

function RecommendationRow({ investigation, rec }: { investigation: Investigation; rec: Recommendation }) {
  const { can } = useAuth();
  const decide = useDecision(investigation.id);
  const [note, setNote] = useState("");
  const decision = investigation.decisions[rec.id];
  return (
    <li className="stack-sm" style={{ borderTop: "1px solid var(--hairline)", paddingTop: 12 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong>{rec.title}</strong>
        <Badge>{humanize(rec.action)}</Badge>
      </div>
      <p className="secondary">{rec.rationale}</p>
      {rec.proposed_mappings && (
        <>
          <JsonView value={rec.proposed_mappings} label="Proposed mappings (not applied)" />
          {rec.validation &&
            (rec.validation.valid ? (
              <Notice tone="good">
                Dry-run check passed against this event’s payload. Nothing has been changed.
              </Notice>
            ) : (
              <Notice tone="warning">
                This proposal does not pass a dry-run against this event’s payload.
                <IssueList issues={rec.validation.issues ?? []} />
              </Notice>
            ))}
        </>
      )}
      {decision ? (
        <Notice tone={decision.decision === "approved" ? "good" : "info"}>
          {decision.decision === "approved" ? "Approved" : "Rejected"}{" "}
          <Time iso={decision.decided_at} relative />
          {decision.note ? ` — “${decision.note}”` : ""}.
          {decision.decision === "approved" && rec.action === "replay_event" && (
            <>
              {" "}
              Approval is a record only. To act on it,{" "}
              <Link to={`/events/${investigation.event_id}`}>replay the event</Link>.
            </>
          )}
        </Notice>
      ) : can("recommendation:decide") ? (
        <div className="row">
          <input
            type="text"
            aria-label={`Note for decision on: ${rec.title}`}
            placeholder="Optional note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            style={{ maxWidth: 280 }}
            maxLength={500}
          />
          <button
            type="button"
            className="btn btn-sm"
            disabled={decide.isPending}
            onClick={() => decide.mutate({ recommendationId: rec.id, decision: "approved", note })}
          >
            Approve
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={decide.isPending}
            onClick={() => decide.mutate({ recommendationId: rec.id, decision: "rejected", note })}
          >
            Reject
          </button>
          <span className="field-hint">Recording a decision does not perform the action.</span>
        </div>
      ) : (
        <span className="field-hint">Awaiting a decision from an operator.</span>
      )}
      <MutationError error={decide.error} />
    </li>
  );
}

export function InvestigationReport({ investigation }: { investigation: Investigation }) {
  const report = investigation.report;
  const isAi = investigation.mode === "ai";
  const notes = investigation.trace?.enforcement_notes ?? [];
  return (
    <div className="stack">
      {isAi ? (
        <Notice tone="info">
          <strong>AI-assisted.</strong> Written by {investigation.provider} ({investigation.model}) from
          stored evidence it read with read-only tools. Treat the likely cause as a hypothesis; observed facts
          are limited to statements that cite a stored record.
        </Notice>
      ) : (
        <Notice tone="info">
          <strong>Rule-based diagnosis.</strong> Produced by fixed rules over stored evidence, without a
          language model.
          {investigation.fallback_reason && (
            <> A model was configured but unavailable ({investigation.fallback_reason}).</>
          )}
        </Notice>
      )}
      {!report ? (
        <Notice tone="critical">
          {investigation.error ?? "The investigation did not produce a report."}
        </Notice>
      ) : (
        <>
          <Card title="Summary">
            <p>{report.summary}</p>
          </Card>
          <div className="grid-2">
            <Card title="Observed facts">
              {report.observed_facts.length === 0 ? (
                <p className="muted">No statement could be tied to stored evidence.</p>
              ) : (
                <ul className="stack-sm" style={{ margin: 0, paddingLeft: 18 }}>
                  {report.observed_facts.map((fact, i) => (
                    <li key={i}>
                      {fact.statement} <EvidenceChips refs={fact.evidence} />
                    </li>
                  ))}
                </ul>
              )}
            </Card>
            <Card
              title="Likely cause (hypothesis)"
              actions={
                <Badge tone={CONFIDENCE_TONE[report.likely_cause.confidence]}>
                  {humanize(report.likely_cause.confidence)} confidence
                </Badge>
              }
            >
              <div className="stack-sm">
                <Badge>{humanize(report.likely_cause.category)}</Badge>
                <p>{report.likely_cause.statement}</p>
                <EvidenceChips refs={report.likely_cause.evidence} />
                {report.likely_cause.evidence.length === 0 && (
                  <span className="field-hint">No stored evidence was cited for this hypothesis.</span>
                )}
              </div>
            </Card>
          </div>
          <div className="grid-2">
            <Card title="Uncertainty">
              {report.uncertainty.length === 0 ? (
                <p className="muted">None stated.</p>
              ) : (
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {report.uncertainty.map((item, i) => (
                    <li key={i}>{item}</li>
                  ))}
                </ul>
              )}
            </Card>
            <Card title="Evidence that would settle it">
              {report.missing_evidence.length === 0 ? (
                <p className="muted">None listed.</p>
              ) : (
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {report.missing_evidence.map((item, i) => (
                    <li key={i}>{item}</li>
                  ))}
                </ul>
              )}
            </Card>
          </div>
          <Card title="Recommendations — each needs a human decision">
            {report.recommendations.length === 0 ? (
              <p className="muted">No recommendations.</p>
            ) : (
              <ul className="stack" style={{ listStyle: "none", margin: 0, padding: 0 }}>
                {report.recommendations.map((rec) => (
                  <RecommendationRow key={rec.id} investigation={investigation} rec={rec} />
                ))}
              </ul>
            )}
          </Card>
        </>
      )}
      <Card title="How this was produced">
        <dl className="kv">
          <dt>Mode</dt>
          <dd>{isAi ? "AI-assisted" : "Deterministic rules"}</dd>
          <dt>Provider / model</dt>
          <dd>{isAi ? `${investigation.provider} / ${investigation.model}` : "None"}</dd>
          <dt>Duration</dt>
          <dd>{formatDuration(investigation.duration_ms)}</dd>
          <dt>Tokens (in / out)</dt>
          <dd>
            {investigation.input_tokens === null
              ? "Not applicable"
              : `${investigation.input_tokens} / ${investigation.output_tokens}`}
          </dd>
          <dt>Requested</dt>
          <dd>
            <Time iso={investigation.created_at} />
          </dd>
          <dt>Evidence cited</dt>
          <dd>{investigation.evidence_refs.length} stored record(s)</dd>
          {notes.length > 0 && (
            <>
              <dt>Server-side corrections</dt>
              <dd>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              </dd>
            </>
          )}
        </dl>
      </Card>
    </div>
  );
}
