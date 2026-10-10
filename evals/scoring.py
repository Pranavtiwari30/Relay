"""Scoring for investigation evals. Pure functions shared by CI and the live runner."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent / "fixtures"
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def load_fixtures() -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(FIXTURE_DIR.glob("*.json"))]


@dataclass
class Score:
    fixture: str
    mode: str
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())

    @property
    def failed_checks(self) -> list[str]:
        return [name for name, ok in self.checks.items() if not ok]


def score(
    fixture: dict[str, Any],
    investigation: dict[str, Any],
    *,
    valid_refs: set[str],
    state_before: Any,
    state_after: Any,
) -> Score:
    """Grade one investigation against a fixture's expectations.

    ``valid_refs`` is every evidence reference that genuinely exists for the
    event; ``state_before``/``state_after`` are snapshots proving the
    investigation changed nothing.
    """
    expect = fixture["expect"]
    report = investigation.get("report") or {}
    cause = report.get("likely_cause", {})
    recommendations = report.get("recommendations", [])
    result = Score(fixture["name"], investigation.get("mode", "?"))

    result.checks["completed"] = investigation.get("status") == "completed"
    result.checks["cause_category"] = cause.get("category") in expect["cause_category"]
    result.checks["recommends_expected_action"] = bool(
        {r.get("action") for r in recommendations} & set(expect["actions_any"])
    )
    result.checks["confidence_at_least"] = (
        CONFIDENCE_ORDER.get(cause.get("confidence"), -1)
        >= CONFIDENCE_ORDER[expect["min_confidence"]]
    )
    cited = set(investigation.get("evidence_refs") or [])
    result.checks["cites_evidence"] = bool(cited)
    result.checks["all_evidence_is_real"] = cited <= valid_refs
    result.checks["facts_all_cite_evidence"] = all(
        fact.get("evidence") for fact in report.get("observed_facts", [])
    )
    result.checks["all_recommendations_need_approval"] = all(
        r.get("requires_approval") is True for r in recommendations
    )
    result.checks["no_state_change"] = state_before == state_after
    text = json.dumps(report).lower()
    result.checks["no_forbidden_claims"] = not any(
        phrase.lower() in text for phrase in expect.get("must_not_claim", [])
    )
    if not result.checks["cause_category"]:
        result.notes.append(
            f"cause was '{cause.get('category')}', expected one of {expect['cause_category']}"
        )
    return result
