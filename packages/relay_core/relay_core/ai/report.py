"""The investigation report contract and its server-side enforcement.

A model's output is treated as untrusted input. ``finalise_report`` enforces,
regardless of what the model wrote:

* the structure below (anything else is rejected);
* every evidence reference must be one the tools actually returned during this
  investigation — a fact with no surviving reference is demoted to an
  explicitly unverified note, never shown as an observed fact;
* every recommendation requires human approval;
* any proposed mapping is re-validated here, not taken on the model's word.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CauseCategory = Literal[
    "destination_unavailable",
    "destination_rejected_payload",
    "authentication",
    "rate_limited",
    "timeout",
    "mapping_error",
    "schema_mismatch",
    "configuration",
    "infrastructure",
    "not_a_failure",
    "unknown",
]
RecommendedAction = Literal[
    "replay_event",
    "update_mapping",
    "update_schema",
    "fix_destination_credentials",
    "fix_destination_configuration",
    "contact_destination_owner",
    "contact_source_owner",
    "wait_and_retry",
    "no_action",
    "other",
]

_CAUSES: Final = list(CauseCategory.__args__)  # type: ignore[attr-defined]
_ACTIONS: Final = list(RecommendedAction.__args__)  # type: ignore[attr-defined]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class Fact(_Model):
    statement: str = Field(min_length=1, max_length=600)
    evidence: list[str] = Field(default_factory=list, max_length=12)


class LikelyCause(_Model):
    category: CauseCategory
    statement: str = Field(min_length=1, max_length=800)
    confidence: Literal["low", "medium", "high"]
    evidence: list[str] = Field(default_factory=list, max_length=12)


class Recommendation(_Model):
    id: str = ""
    action: RecommendedAction
    title: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=800)
    requires_approval: bool = True
    proposed_mappings: list[dict[str, Any]] | None = Field(default=None, max_length=200)
    validation: dict[str, Any] | None = None


class InvestigationReport(_Model):
    summary: str = Field(min_length=1, max_length=1200)
    observed_facts: list[Fact] = Field(default_factory=list, max_length=20)
    likely_cause: LikelyCause
    uncertainty: list[str] = Field(default_factory=list, max_length=12)
    missing_evidence: list[str] = Field(default_factory=list, max_length=12)
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=6)


class ReportRejectedError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


_REF = {
    "type": "array",
    "items": {"type": "string"},
    "description": "evidence_ref values copied exactly from tool results.",
}

# JSON Schema advertised to models for the ``submit_report`` tool.
REPORT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "required": [
        "summary",
        "observed_facts",
        "likely_cause",
        "uncertainty",
        "missing_evidence",
        "recommendations",
    ],
    "properties": {
        "summary": {
            "type": "string",
            "description": "Two or three plain sentences for an on-call operator.",
        },
        "observed_facts": {
            "type": "array",
            "description": "Only things directly read from tool results. Each must cite evidence.",
            "items": {
                "type": "object",
                "required": ["statement", "evidence"],
                "properties": {"statement": {"type": "string"}, "evidence": _REF},
            },
        },
        "likely_cause": {
            "type": "object",
            "description": "Your best hypothesis. This is an inference, not an observed fact.",
            "required": ["category", "statement", "confidence", "evidence"],
            "properties": {
                "category": {"type": "string", "enum": _CAUSES},
                "statement": {"type": "string"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "evidence": _REF,
            },
        },
        "uncertainty": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What you could not determine.",
        },
        "missing_evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Evidence that would confirm or refute the hypothesis.",
        },
        "recommendations": {
            "type": "array",
            "description": "Actions for a human to consider. You cannot perform any of them.",
            "items": {
                "type": "object",
                "required": ["action", "title", "rationale"],
                "properties": {
                    "action": {"type": "string", "enum": _ACTIONS},
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "proposed_mappings": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Only with action update_mapping: the complete replacement mapping list.",
                    },
                },
            },
        },
    },
}


def _clean(text: str) -> str:
    return "".join(ch if ch >= " " or ch == "\n" else " " for ch in text).strip()


def _dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        cleaned = _clean(str(item))[:600]
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def finalise_report(
    raw: Any,
    *,
    collected_refs: set[str],
    validate_mappings: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate and harden a raw report. Returns ``(report, enforcement_notes)``."""
    try:
        report = InvestigationReport.model_validate(raw)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in error['loc']) or '$'}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False, include_context=False)[
                :12
            ]
        ]
        raise ReportRejectedError(problems) from exc

    notes: list[str] = []
    facts: list[Fact] = []
    unverified: list[str] = []
    for fact in report.observed_facts:
        valid = [ref for ref in dict.fromkeys(fact.evidence) if ref in collected_refs]
        dropped = len(set(fact.evidence)) - len(valid)
        if dropped:
            notes.append(f"removed {dropped} evidence reference(s) that no tool returned")
        if valid:
            facts.append(Fact(statement=_clean(fact.statement), evidence=valid))
        else:
            unverified.append(
                f"Unverified claim (no stored evidence cited): {_clean(fact.statement)}"
            )
    if unverified:
        notes.append(f"demoted {len(unverified)} claim(s) with no valid evidence to 'uncertainty'")

    cause = report.likely_cause
    cause_refs = [ref for ref in dict.fromkeys(cause.evidence) if ref in collected_refs]
    confidence = cause.confidence
    if not cause_refs and confidence != "low":
        confidence = "low"
        notes.append("lowered confidence to 'low': the likely cause cited no valid evidence")

    recommendations: list[Recommendation] = []
    for index, rec in enumerate(report.recommendations, start=1):
        validation = None
        mappings = rec.proposed_mappings if rec.action == "update_mapping" else None
        if mappings and validate_mappings is not None:
            validation = validate_mappings(mappings)
        if not rec.requires_approval:
            notes.append("forced requires_approval=true on a recommendation")
        recommendations.append(
            Recommendation(
                id=f"rec-{index}",
                action=rec.action,
                title=_clean(rec.title),
                rationale=_clean(rec.rationale),
                requires_approval=True,
                proposed_mappings=mappings,
                validation=validation,
            )
        )

    final = InvestigationReport(
        summary=_clean(report.summary),
        observed_facts=facts,
        likely_cause=LikelyCause(
            category=cause.category,
            statement=_clean(cause.statement),
            confidence=confidence,
            evidence=cause_refs,
        ),
        uncertainty=_dedupe([*report.uncertainty, *unverified])[:20],
        missing_evidence=_dedupe(report.missing_evidence),
        recommendations=recommendations,
    )
    return final.model_dump(mode="json"), list(dict.fromkeys(notes))


def referenced_evidence(report: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for fact in report.get("observed_facts", []):
        refs.extend(fact.get("evidence", []))
    refs.extend(report.get("likely_cause", {}).get("evidence", []))
    return list(dict.fromkeys(refs))
