"""Investigation orchestration: evidence gathering, model fallback, persistence, approvals.

An investigation runs only when an operator asks for one. Normal event
processing never touches this module, so Relay behaves identically when every
model provider is absent or down.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, tuple_
from sqlalchemy.orm import Session

from relay_core.ai.deterministic import diagnose
from relay_core.ai.investigator import AiInvestigator, InvestigationUnavailableError
from relay_core.ai.providers.base import ChatProvider
from relay_core.ai.report import ReportRejectedError, finalise_report, referenced_evidence
from relay_core.ai.tools import ToolContext, evaluate_mappings
from relay_core.config import Settings
from relay_core.db.models import Event, IntegrationVersion, Investigation
from relay_core.db.session import SessionFactory, session_scope, utcnow
from relay_core.domain.pagination import Page, clamp_limit, decode_cursor, encode_cursor
from relay_core.domain.transform.pipeline import parse_config
from relay_core.errors import ConflictError, NotFoundError
from relay_core.logging import bind_log_context
from relay_core.services.audit import record_audit
from relay_core.services.event_state import Actor

logger = logging.getLogger(__name__)


def provider_status(settings: Settings) -> list[dict[str, Any]]:
    """Which configured provider slots are usable. Never includes credential values."""
    status = []
    for name in settings.ai_providers:
        if name == "ollama":
            status.append(
                {
                    "name": name,
                    "model": settings.ai_ollama_model,
                    "configured": True,
                    "kind": "local",
                }
            )
        elif name == "openai_compat":
            ready = bool(
                settings.ai_openai_compat_base_url
                and settings.ai_openai_compat_api_key
                and settings.ai_openai_compat_model
            )
            status.append(
                {
                    "name": name,
                    "model": settings.ai_openai_compat_model,
                    "configured": ready,
                    "kind": "hosted",
                }
            )
        elif name == "anthropic":
            status.append(
                {
                    "name": name,
                    "model": settings.ai_anthropic_model,
                    "configured": settings.ai_anthropic_api_key is not None,
                    "kind": "hosted",
                }
            )
    return status


def build_providers(settings: Settings) -> list[ChatProvider]:
    """Instantiate the configured provider slots in priority order, skipping incomplete ones."""
    providers: list[ChatProvider] = []
    for name in settings.ai_providers:
        if name == "ollama":
            from relay_core.ai.providers.http_chat import OllamaProvider

            providers.append(OllamaProvider(settings.ai_ollama_base_url, settings.ai_ollama_model))
        elif name == "openai_compat":
            if (
                settings.ai_openai_compat_base_url
                and settings.ai_openai_compat_api_key
                and settings.ai_openai_compat_model
            ):
                from relay_core.ai.providers.http_chat import OpenAICompatProvider

                providers.append(
                    OpenAICompatProvider(
                        settings.ai_openai_compat_base_url,
                        settings.ai_openai_compat_api_key.get_secret_value(),
                        settings.ai_openai_compat_model,
                    )
                )
            else:
                logger.warning(
                    "AI provider 'openai_compat' is listed but not fully configured; skipping"
                )
        elif name == "anthropic":
            if settings.ai_anthropic_api_key is not None:
                from relay_core.ai.providers.anthropic_provider import AnthropicProvider

                providers.append(
                    AnthropicProvider(
                        settings.ai_anthropic_api_key.get_secret_value(),
                        settings.ai_anthropic_model,
                        effort=settings.ai_anthropic_effort,
                    )
                )
            else:
                logger.warning("AI provider 'anthropic' is listed but has no API key; skipping")
    return providers


class InvestigationService:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings,
        *,
        providers: list[ChatProvider] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._providers = build_providers(settings) if providers is None else providers
        self._clock = clock

    @property
    def providers(self) -> list[ChatProvider]:
        return self._providers

    def run(self, *, workspace_id: uuid.UUID, event_id: uuid.UUID, actor: Actor) -> Investigation:
        with bind_log_context(event_id=str(event_id), workspace_id=str(workspace_id)):
            return self._run(workspace_id, event_id, actor)

    def _run(self, workspace_id: uuid.UUID, event_id: uuid.UUID, actor: Actor) -> Investigation:
        started = time.monotonic()
        created_at = self._clock()
        settings = self._settings
        mode, provider, model = "deterministic", None, None
        fallback_reason: str | None = None
        trace: dict[str, Any] = {"steps": []}
        input_tokens = output_tokens = None
        report: dict[str, Any] | None = None
        notes: list[str] = []
        error: str | None = None
        collected: set[str] = set()

        with self._session_factory() as reader:
            # The database, not just our code, enforces that investigation reads
            # cannot write: any INSERT/UPDATE/DELETE on this transaction fails.
            reader.execute(text("SET TRANSACTION READ ONLY"))
            event = reader.execute(
                select(Event).where(Event.id == event_id, Event.workspace_id == workspace_id)
            ).scalar_one_or_none()
            if event is None:
                raise NotFoundError("event not found")
            version = reader.get_one(IntegrationVersion, event.integration_version_id)
            config, _ = parse_config(version.config)

            def validate_mappings(mappings: list[dict[str, Any]]) -> dict[str, Any]:
                if config is None:
                    return {
                        "valid": False,
                        "issues": [
                            {
                                "code": "invalid_config",
                                "path": "$",
                                "message": "stored configuration is unreadable",
                            }
                        ],
                    }
                outcome = evaluate_mappings(event, config, mappings)
                outcome.pop("output_preview_untrusted", None)
                return outcome

            ctx = ToolContext(
                session=reader,
                workspace_id=workspace_id,
                event_id=event_id,
                max_chars=settings.ai_max_tool_result_chars,
            )

            def check(candidate: Any) -> list[str]:
                try:
                    finalise_report(
                        candidate, collected_refs=ctx.evidence, validate_mappings=validate_mappings
                    )
                except ReportRejectedError as exc:
                    return exc.problems
                return []

            if self._providers:
                investigator = AiInvestigator(
                    providers=self._providers,
                    max_steps=settings.ai_max_steps,
                    request_timeout=settings.ai_request_timeout_seconds,
                    total_timeout=settings.ai_total_timeout_seconds,
                    max_output_tokens=settings.ai_max_output_tokens,
                    max_tool_result_chars=settings.ai_max_tool_result_chars,
                )
                try:
                    result = investigator.investigate(ctx, str(event_id), check_report=check)
                    report, notes = finalise_report(
                        result.raw_report,
                        collected_refs=ctx.evidence,
                        validate_mappings=validate_mappings,
                    )
                    mode, provider, model = "ai", result.provider, result.model
                    input_tokens, output_tokens = (
                        result.usage.input_tokens,
                        result.usage.output_tokens,
                    )
                    trace["steps"] = result.trace
                except InvestigationUnavailableError as exc:
                    trace["provider_attempts"] = exc.attempts
                    fallback_reason = "every configured model provider failed: " + "; ".join(
                        f"{a['provider']} ({a['error_kind']})" for a in exc.attempts
                    )
                except Exception:
                    # A bug in the AI path must never cost the operator their diagnosis.
                    logger.exception("AI investigation crashed; falling back to rules")
                    fallback_reason = "the AI investigation failed unexpectedly"
                    ctx.evidence.clear()
            if report is None:
                try:
                    report, notes = finalise_report(
                        diagnose(ctx, str(event_id)),
                        collected_refs=ctx.evidence,
                        validate_mappings=validate_mappings,
                    )
                except Exception:
                    logger.exception("deterministic diagnosis failed")
                    error = "the diagnosis could not be completed"
            collected = set(ctx.evidence)
            reader.rollback()

        trace["enforcement_notes"] = notes
        trace["evidence_collected"] = len(collected)
        investigation = Investigation(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            event_id=event_id,
            requested_by=actor.user_id,
            status="completed" if report is not None else "failed",
            mode=mode,
            provider=provider,
            model=model,
            report=report,
            evidence_refs=referenced_evidence(report) if report else [],
            trace=trace,
            decisions={},
            fallback_reason=fallback_reason,
            error=error,
            duration_ms=int((time.monotonic() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            created_at=created_at,
            completed_at=self._clock(),
        )
        with session_scope(self._session_factory) as writer:
            writer.add(investigation)
            record_audit(
                writer,
                workspace_id=workspace_id,
                actor=actor,
                action="investigation.run",
                entity_type="event",
                entity_id=event_id,
                details={
                    "investigation_id": str(investigation.id),
                    "mode": mode,
                    "provider": provider,
                    "model": model,
                    "status": investigation.status,
                    "fell_back": fallback_reason is not None,
                },
            )
        logger.info(
            "investigation finished",
            extra={
                "investigation_id": str(investigation.id),
                "mode": mode,
                "provider": provider,
                "status": investigation.status,
                "duration_ms": investigation.duration_ms,
                "fell_back": fallback_reason is not None,
            },
        )
        return investigation


def get_investigation(
    session: Session, workspace_id: uuid.UUID, investigation_id: uuid.UUID
) -> Investigation:
    row = session.execute(
        select(Investigation).where(
            Investigation.id == investigation_id, Investigation.workspace_id == workspace_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("investigation not found")
    return row


def list_investigations(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    event_id: uuid.UUID | None,
    cursor: str | None,
    limit: int | None,
) -> Page[Investigation]:
    size = clamp_limit(limit)
    query = select(Investigation).where(Investigation.workspace_id == workspace_id)
    if event_id is not None:
        query = query.where(Investigation.event_id == event_id)
    if cursor:
        timestamp, row_id = decode_cursor(cursor)
        query = query.where(
            tuple_(Investigation.created_at, Investigation.id) < tuple_(timestamp, row_id)
        )
    rows = list(
        session.execute(
            query.order_by(Investigation.created_at.desc(), Investigation.id.desc()).limit(size + 1)
        ).scalars()
    )
    next_cursor = None
    if len(rows) > size:
        rows = rows[:size]
        next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id)
    return Page(rows, next_cursor)


def record_decision(
    session: Session,
    *,
    actor: Actor,
    workspace_id: uuid.UUID,
    investigation_id: uuid.UUID,
    recommendation_id: str,
    decision: str,
    note: str,
    now: datetime,
) -> Investigation:
    """Record a human's approval or rejection of one recommendation.

    Recording a decision executes nothing. Acting on an approved recommendation
    (for example replaying the event) is a separate request made by the
    operator through the normal, permission-checked endpoint.
    """
    investigation = session.execute(
        select(Investigation)
        .where(Investigation.id == investigation_id, Investigation.workspace_id == workspace_id)
        .with_for_update()
    ).scalar_one_or_none()
    if investigation is None:
        raise NotFoundError("investigation not found")
    recommendations = (investigation.report or {}).get("recommendations", [])
    match = next((r for r in recommendations if r.get("id") == recommendation_id), None)
    if match is None:
        raise NotFoundError("recommendation not found")
    if recommendation_id in investigation.decisions:
        raise ConflictError(
            "a decision was already recorded for this recommendation", code="already_decided"
        )
    investigation.decisions = {
        **investigation.decisions,
        recommendation_id: {
            "decision": decision,
            "note": note.strip()[:500],
            "decided_by": str(actor.user_id) if actor.user_id else None,
            "decided_at": now.isoformat(),
        },
    }
    record_audit(
        session,
        workspace_id=workspace_id,
        actor=actor,
        action=f"investigation.recommendation_{decision}",
        entity_type="investigation",
        entity_id=investigation.id,
        details={
            "recommendation_id": recommendation_id,
            "recommended_action": match.get("action"),
            "event_id": str(investigation.event_id),
            "investigation_mode": investigation.mode,
            "note": note.strip()[:200],
        },
    )
    return investigation
