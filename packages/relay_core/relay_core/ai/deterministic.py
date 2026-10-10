"""Rule-based diagnosis: the always-available, model-free investigation path.

Produces the same report shape as the AI investigator from the same read-only
tools, so the UI, audit trail and evidence checks are identical. It runs when
no model provider is configured and whenever every provider fails.
"""

from __future__ import annotations

from typing import Any

from relay_core.ai.tools import ToolContext, call_tool

_GENERIC_MISSING = [
    "Destination-side logs for the request (correlate with the X-Relay-Event-Id / X-Relay-Correlation-Id headers).",
]


def _read(ctx: ToolContext, name: str, event_id: str, **extra: Any) -> dict[str, Any]:
    outcome = call_tool(ctx, name, {"event_id": event_id, **extra})
    return outcome.content if outcome.ok else {}


def _rec(action: str, title: str, rationale: str) -> dict[str, Any]:
    return {"action": action, "title": title, "rationale": rationale}


def diagnose(ctx: ToolContext, event_id: str) -> dict[str, Any]:
    event = _read(ctx, "get_event", event_id)
    attempts_view = _read(ctx, "list_delivery_attempts", event_id)
    errors = _read(ctx, "get_transformation_errors", event_id)
    config = _read(ctx, "get_integration_config", event_id)
    examples = _read(ctx, "list_recent_successful_examples", event_id, limit=2)

    event_ref = event.get("evidence_ref", f"event:{event_id}")
    status = event.get("status", "UNKNOWN")
    attempts: list[dict[str, Any]] = [
        a for a in attempts_view.get("attempts", []) if a.get("status") != "in_progress"
    ]
    current = [a for a in attempts if a.get("cycle") == event.get("delivery_cycle")] or attempts
    facts: list[dict[str, Any]] = [
        {
            "statement": f"The event is in status {status} after {event.get('attempt_count', 0)} of "
            f"{event.get('max_attempts', '?')} allowed delivery attempt(s) in delivery cycle "
            f"{event.get('delivery_cycle', 1)}.",
            "evidence": [event_ref],
        }
    ]
    uncertainty: list[str] = [
        "This diagnosis was produced by fixed rules over stored evidence, without a language model."
    ]
    missing = list(_GENERIC_MISSING)
    recommendations: list[dict[str, Any]] = []
    category, confidence, statement = (
        "unknown",
        "low",
        "The stored evidence does not match a known failure pattern.",
    )
    cause_refs = [event_ref]

    if config.get("newer_version_available"):
        facts.append(
            {
                "statement": f"The event was processed with configuration version {config.get('version_used')}, "
                f"but version {config.get('latest_version')} now exists.",
                "evidence": [config["evidence_ref"]],
            }
        )

    if status == "VALIDATION_FAILED":
        stage = errors.get("failure_stage") or event.get("failure_stage") or "unknown"
        issues = errors.get("issues", [])
        for issue in issues[:5]:
            facts.append(
                {
                    "statement": f"Validation issue at stage '{stage}': {issue.get('path')} — {issue.get('message')}.",
                    "evidence": [event_ref],
                }
            )
        example_list = examples.get("examples", [])
        payload = event.get("payload_untrusted")
        if example_list and isinstance(payload, dict):
            reference = example_list[0].get("payload_untrusted")
            if isinstance(reference, dict):
                absent = sorted(set(reference) - set(payload))[:8]
                if absent:
                    facts.append(
                        {
                            "statement": "Top-level field(s) present in a recently delivered event but absent "
                            f"from this payload: {', '.join(absent)}.",
                            "evidence": [event_ref, example_list[0]["evidence_ref"]],
                        }
                    )
        cause_refs = [event_ref] + ([config["evidence_ref"]] if config.get("evidence_ref") else [])
        confidence = "high" if issues else "medium"
        if stage == "input_schema":
            category = "schema_mismatch"
            statement = "The inbound payload does not satisfy the integration's input schema, so it was rejected before any delivery was attempted."
            recommendations = [
                _rec(
                    "contact_source_owner",
                    "Ask the source system's owner about the payload shape",
                    "The sender produced a payload that violates the agreed input schema.",
                ),
                _rec(
                    "update_schema",
                    "Review whether the input schema is stricter than intended",
                    "If the payload is legitimate, the schema — not the sender — needs to change.",
                ),
            ]
        elif stage == "transform":
            category = "mapping_error"
            statement = "The payload passed input validation but a mapping could not be applied (missing required field or failed conversion)."
            recommendations = [
                _rec(
                    "update_mapping",
                    "Fix the mapping named in the issue list",
                    "A required source field or type conversion did not hold for this payload.",
                ),
                _rec(
                    "update_schema",
                    "Tighten the input schema to reject this shape earlier",
                    "The input schema accepted a value the mapping cannot convert.",
                ),
            ]
        elif stage == "output_schema":
            category = "schema_mismatch"
            statement = (
                "The mappings ran, but their result violates the integration's output schema."
            )
            recommendations = [
                _rec(
                    "update_mapping",
                    "Align the mappings with the output schema",
                    "The transformed payload does not meet the destination contract.",
                )
            ]
        else:
            category = "configuration"
            statement = (
                "The stored integration configuration could not be used to process this event."
            )
            recommendations = [
                _rec(
                    "fix_destination_configuration",
                    "Re-validate the integration configuration",
                    "Processing stopped before validation could run.",
                )
            ]
        recommendations.append(
            _rec(
                "replay_event",
                "Replay this event after the fix",
                "Replaying with the current configuration re-runs validation and delivery; history is preserved.",
            )
        )
        missing = ["Confirmation from the source system's owner of the intended payload format."]

    elif status in ("DELIVERED", "FILTERED"):
        category, confidence = "not_a_failure", "high"
        statement = (
            "The destination accepted this event; there is no failure to diagnose."
            if status == "DELIVERED"
            else "The event was intentionally skipped by the integration's filter rules; it is not a failure."
        )
        recommendations = [_rec("no_action", "No action needed", statement)]
        missing = []

    elif current:
        last = current[-1]
        refs = [a["evidence_ref"] for a in current]
        cause_refs = refs[-3:]
        kinds = [a.get("failure_category") for a in current if a.get("failure_category")]
        http = last.get("http_status")
        facts.append(
            {
                "statement": "Attempt outcomes in this cycle, in order: "
                + ", ".join(
                    f"#{a.get('attempt_number')} {a.get('failure_category') or a.get('status')}"
                    + (f" (HTTP {a['http_status']})" if a.get("http_status") else "")
                    for a in current
                )
                + ".",
                "evidence": refs,
            }
        )
        if last.get("error_message"):
            facts.append(
                {
                    "statement": f"The most recent attempt recorded: {last['error_message']}",
                    "evidence": [last["evidence_ref"]],
                }
            )
        uniform = len(set(kinds)) == 1 and len(kinds) == len(current)
        confidence = "high" if uniform and len(current) > 1 else "medium"
        kind = last.get("failure_category")
        replay = _rec(
            "replay_event",
            "Replay the event once the cause is resolved",
            "A replay opens a new delivery cycle with a fresh attempt budget and keeps the existing history.",
        )
        if kind == "auth":
            category, confidence = "authentication", "high"
            statement = f"The destination rejected Relay's credentials (HTTP {http}). Retrying cannot succeed until the credential is corrected."
            recommendations = [
                _rec(
                    "fix_destination_credentials",
                    "Check the destination credential and its secret reference",
                    "An authentication failure is permanent until the secret is rotated or corrected.",
                ),
                replay,
            ]
        elif kind == "http_4xx":
            category = "destination_rejected_payload"
            statement = f"The destination refused the request as invalid (HTTP {http}). This is a permanent rejection of this payload, not an outage."
            recommendations = [
                _rec(
                    "update_mapping",
                    "Compare the transformed payload with the destination's contract",
                    "The response summary usually names the field the destination objected to.",
                ),
                _rec(
                    "contact_destination_owner",
                    "Confirm the expected format with the destination's owner",
                    "Their validation rules may have changed.",
                ),
                replay,
            ]
        elif kind == "http_429":
            category = "rate_limited"
            statement = "The destination throttled Relay (HTTP 429) on every attempt until the retry budget ran out."
            recommendations = [
                _rec(
                    "wait_and_retry",
                    "Wait for the destination's rate limit to clear",
                    "Throttling is transient.",
                ),
                replay,
            ]
        elif kind == "http_5xx":
            category = "destination_unavailable"
            statement = f"The destination returned server errors (HTTP {http}); it was failing or overloaded during the retry window."
            recommendations = [
                _rec(
                    "contact_destination_owner",
                    "Check the destination's health or status page",
                    "Server errors originate on the destination's side.",
                ),
                replay,
            ]
        elif kind in ("timeout", "http_408"):
            category = "timeout"
            statement = "The destination did not answer within the configured delivery timeout."
            uncertainty.append(
                "A timed-out request may still have been processed by the destination. Replays reuse the same idempotency key so a destination that honours it will not apply the event twice."
            )
            recommendations = [
                _rec(
                    "contact_destination_owner",
                    "Ask whether the destination received the request",
                    "Timeouts are ambiguous: only the destination knows whether it acted.",
                ),
                replay,
            ]
        elif kind in ("transport", "invalid_response"):
            category = "destination_unavailable"
            statement = "Relay could not complete an HTTP exchange with the destination (DNS, connection, or protocol failure)."
            recommendations = [
                _rec(
                    "fix_destination_configuration",
                    "Verify the destination URL and that the host is reachable",
                    "No valid HTTP response was received.",
                ),
                replay,
            ]
        elif kind in ("blocked_destination", "redirect", "configuration"):
            category, confidence = "configuration", "high"
            statement = {
                "blocked_destination": "Relay's SSRF protection blocked the destination URL because it resolves to a non-public address.",
                "redirect": "The destination answered with a redirect. Relay never follows redirects, so the configured URL must be the final one.",
            }.get(
                kind,
                "The destination connection is misconfigured or its credential reference could not be resolved.",
            )
            recommendations = [
                _rec(
                    "fix_destination_configuration",
                    "Correct the destination connection",
                    "This failure is permanent until the configuration changes.",
                ),
                replay,
            ]
        elif kind in ("worker_interrupted", "queue_exhausted"):
            category = "infrastructure"
            statement = "Processing was interrupted inside Relay (worker restart or queue redelivery limit), not by the destination."
            uncertainty.append(
                "An interrupted attempt may or may not have reached the destination."
            )
            recommendations = [replay]
        elif kind == "integration_disabled":
            category, confidence = "configuration", "high"
            statement = "The integration was disabled before this event could be delivered."
            recommendations = [
                _rec(
                    "fix_destination_configuration",
                    "Re-activate the integration if delivery is still wanted",
                    "Events for a disabled integration are dead-lettered.",
                ),
                replay,
            ]
        if status == "RETRY_SCHEDULED":
            uncertainty.append(
                "Retries are still scheduled; the event may yet be delivered without intervention."
            )
            recommendations = [
                _rec(
                    "wait_and_retry",
                    "Let the scheduled retry run",
                    f"Next attempt is due at {event.get('next_attempt_at')}.",
                ),
                *[r for r in recommendations if r["action"] != "replay_event"],
            ]

    elif status == "DEAD_LETTERED":
        kind = event.get("failure_category")
        category = (
            "configuration"
            if kind in ("integration_disabled", "configuration")
            else "infrastructure"
        )
        confidence = "medium"
        statement = (
            event.get("last_error")
            or "The event was dead-lettered before any delivery attempt completed."
        )
        recommendations = [
            _rec(
                "replay_event",
                "Replay the event once the cause is resolved",
                "No delivery attempt completed, so nothing reached the destination.",
            )
        ]

    else:
        category, confidence = "not_a_failure", "medium"
        statement = (
            f"The event is still in flight (status {status}); no failure has been recorded yet."
        )
        recommendations = [
            _rec(
                "wait_and_retry",
                "Wait for processing to finish",
                "The pipeline has not reached an outcome for this event.",
            )
        ]
        missing = []

    return {
        "summary": statement,
        "observed_facts": facts,
        "likely_cause": {
            "category": category,
            "statement": statement,
            "confidence": confidence,
            "evidence": cause_refs,
        },
        "uncertainty": uncertainty,
        "missing_evidence": missing,
        "recommendations": recommendations,
    }
