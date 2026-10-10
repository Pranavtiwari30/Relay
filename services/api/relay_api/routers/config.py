"""Connections and integrations: the configuration half of the control plane."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends

from relay_api import schemas
from relay_api.deps import Context, require
from relay_api.serializers import connection_out, integration_detail, integrations_out, version_out
from relay_core.domain.roles import Permission
from relay_core.domain.transform.pipeline import has_errors, parse_config
from relay_core.services import connections as connection_service
from relay_core.services import integrations as integration_service

router = APIRouter(prefix="/api/v1")

# --- connections -----------------------------------------------------------


@router.get(
    "/adapters",
    response_model=schemas.AdapterCatalog,
    tags=["connections"],
    summary="Registered adapter types",
)
def adapters(context: Context = Depends(require(Permission.CONNECTION_READ))) -> dict[str, Any]:
    registry = context.container.registry
    return {"sources": registry.source_types, "destinations": registry.destination_types}


@router.get(
    "/connections",
    response_model=list[schemas.ConnectionOut],
    tags=["connections"],
    summary="List connections",
)
def list_connections(
    context: Context = Depends(require(Permission.CONNECTION_READ)),
) -> list[dict[str, Any]]:
    c = context.container
    return [
        connection_out(row, c.registry, c.secrets)
        for row in connection_service.list_connections(context.db, context.workspace_id)
    ]


@router.post(
    "/connections",
    response_model=schemas.ConnectionOut,
    status_code=201,
    tags=["connections"],
    summary="Create a connection (administrators)",
)
def create_connection(
    body: schemas.ConnectionCreate, context: Context = Depends(require(Permission.CONNECTION_WRITE))
) -> dict[str, Any]:
    c = context.container
    connection, _ = connection_service.create_connection(
        context.db,
        actor=context.actor,
        workspace_id=context.workspace_id,
        registry=c.registry,
        name=body.name,
        direction=body.direction,
        adapter_type=body.adapter_type,
        config=body.config,
        secret_ref=body.secret_ref or None,
    )
    context.db.commit()
    return connection_out(connection, c.registry, c.secrets)


@router.get(
    "/connections/{connection_id}",
    response_model=schemas.ConnectionOut,
    tags=["connections"],
    summary="Get a connection",
)
def get_connection(
    connection_id: uuid.UUID, context: Context = Depends(require(Permission.CONNECTION_READ))
) -> dict[str, Any]:
    c = context.container
    return connection_out(
        connection_service.get_connection(context.db, context.workspace_id, connection_id),
        c.registry,
        c.secrets,
    )


@router.patch(
    "/connections/{connection_id}",
    response_model=schemas.ConnectionOut,
    tags=["connections"],
    summary="Update a connection (administrators)",
)
def update_connection(
    connection_id: uuid.UUID,
    body: schemas.ConnectionUpdate,
    context: Context = Depends(require(Permission.CONNECTION_WRITE)),
) -> dict[str, Any]:
    c = context.container
    # An explicit null clears the secret reference; an omitted field leaves it alone.
    secret_ref = (
        (body.secret_ref or None)
        if "secret_ref" in body.model_fields_set
        else connection_service.UNSET
    )
    connection, _ = connection_service.update_connection(
        context.db,
        actor=context.actor,
        workspace_id=context.workspace_id,
        connection_id=connection_id,
        registry=c.registry,
        name=body.name,
        config=body.config,
        secret_ref=secret_ref,
        is_active=body.is_active,
    )
    context.db.commit()
    return connection_out(connection, c.registry, c.secrets)


@router.delete(
    "/connections/{connection_id}",
    status_code=204,
    tags=["connections"],
    summary="Delete an unused connection (administrators)",
)
def delete_connection(
    connection_id: uuid.UUID, context: Context = Depends(require(Permission.CONNECTION_WRITE))
) -> None:
    connection_service.delete_connection(
        context.db,
        actor=context.actor,
        workspace_id=context.workspace_id,
        connection_id=connection_id,
    )
    context.db.commit()


# --- integrations ----------------------------------------------------------


@router.get(
    "/integrations",
    response_model=list[schemas.IntegrationOut],
    tags=["integrations"],
    summary="List integrations",
)
def list_integrations(
    context: Context = Depends(require(Permission.INTEGRATION_READ)),
) -> list[dict[str, Any]]:
    return integrations_out(
        context.db, integration_service.list_integrations(context.db, context.workspace_id)
    )


@router.post(
    "/integrations",
    response_model=schemas.IntegrationDetail,
    status_code=201,
    tags=["integrations"],
    summary="Create a draft integration (administrators)",
)
def create_integration(
    body: schemas.IntegrationCreate,
    context: Context = Depends(require(Permission.INTEGRATION_WRITE)),
) -> dict[str, Any]:
    integration = integration_service.create_integration(
        context.db,
        actor=context.actor,
        workspace_id=context.workspace_id,
        name=body.name,
        description=body.description,
        source_connection_id=body.source_connection_id,
        destination_connection_id=body.destination_connection_id,
        config=body.config,
        change_note=body.change_note,
    )
    context.db.commit()
    return integration_detail(context.db, integration)


@router.post(
    "/integrations/preview",
    response_model=schemas.PreviewResult,
    tags=["integrations"],
    summary="Dry-run a configuration against a sample payload",
)
def preview(
    body: schemas.PreviewRequest, context: Context = Depends(require(Permission.INTEGRATION_READ))
) -> dict[str, Any]:
    return integration_service.preview_transformation(body.config, body.payload)


@router.get(
    "/integrations/{integration_id}",
    response_model=schemas.IntegrationDetail,
    tags=["integrations"],
    summary="Get an integration with its current configuration",
)
def get_integration(
    integration_id: uuid.UUID, context: Context = Depends(require(Permission.INTEGRATION_READ))
) -> dict[str, Any]:
    return integration_detail(
        context.db,
        integration_service.get_integration(context.db, context.workspace_id, integration_id),
    )


@router.patch(
    "/integrations/{integration_id}",
    response_model=schemas.IntegrationDetail,
    tags=["integrations"],
    summary="Update an integration; a configuration change creates a new version (administrators)",
)
def update_integration(
    integration_id: uuid.UUID,
    body: schemas.IntegrationUpdate,
    context: Context = Depends(require(Permission.INTEGRATION_WRITE)),
) -> dict[str, Any]:
    c = context.container
    integration = integration_service.update_integration(
        context.db,
        actor=context.actor,
        workspace_id=context.workspace_id,
        integration_id=integration_id,
        registry=c.registry,
        secrets=c.secrets,
        name=body.name,
        description=body.description,
        source_connection_id=body.source_connection_id,
        destination_connection_id=body.destination_connection_id,
        config=body.config,
        change_note=body.change_note,
    )
    context.db.commit()
    return integration_detail(context.db, integration)


@router.get(
    "/integrations/{integration_id}/versions",
    response_model=list[schemas.VersionOut],
    tags=["integrations"],
    summary="Configuration revision history",
)
def list_versions(
    integration_id: uuid.UUID, context: Context = Depends(require(Permission.INTEGRATION_READ))
) -> list[dict[str, Any]]:
    integration = integration_service.get_integration(
        context.db, context.workspace_id, integration_id
    )
    versions = integration_service.list_versions(context.db, context.workspace_id, integration_id)
    return [version_out(v, integration.current_version_id) for v in versions]


@router.post(
    "/integrations/{integration_id}/validate",
    response_model=schemas.ValidationResult,
    tags=["integrations"],
    summary="Check whether an integration can be activated",
)
def validate(
    integration_id: uuid.UUID,
    body: schemas.ValidateRequest | None = None,
    context: Context = Depends(require(Permission.INTEGRATION_READ)),
) -> dict[str, Any]:
    c = context.container
    integration = integration_service.get_integration(
        context.db, context.workspace_id, integration_id
    )
    raw = (
        body.config
        if body and body.config is not None
        else integration_detail(context.db, integration)["config"]
    )
    config, issues = parse_config(raw)
    if config is not None:
        issues = integration_service.activation_issues(
            context.db, integration, config, c.registry, c.secrets
        )
    return {"valid": not has_errors(issues), "issues": [issue.as_dict() for issue in issues]}


def _change_status(
    integration_id: uuid.UUID, action: integration_service.StatusAction, context: Context
) -> dict[str, Any]:
    c = context.container
    integration, affected = integration_service.change_status(
        context.db,
        actor=context.actor,
        workspace_id=context.workspace_id,
        integration_id=integration_id,
        action=action,
        registry=c.registry,
        secrets=c.secrets,
        now=context.now(),
    )
    context.db.commit()
    return {
        "integration": integrations_out(context.db, [integration])[0],
        "held_events_affected": affected,
    }


@router.post(
    "/integrations/{integration_id}/activate",
    response_model=schemas.StatusChangeResult,
    tags=["integrations"],
    summary="Validate and activate (administrators)",
)
def activate(
    integration_id: uuid.UUID, context: Context = Depends(require(Permission.INTEGRATION_WRITE))
) -> dict[str, Any]:
    return _change_status(integration_id, "activate", context)


@router.post(
    "/integrations/{integration_id}/disable",
    response_model=schemas.StatusChangeResult,
    tags=["integrations"],
    summary="Disable; held events are dead-lettered (administrators)",
)
def disable(
    integration_id: uuid.UUID, context: Context = Depends(require(Permission.INTEGRATION_WRITE))
) -> dict[str, Any]:
    return _change_status(integration_id, "disable", context)


@router.post(
    "/integrations/{integration_id}/pause",
    response_model=schemas.StatusChangeResult,
    tags=["integrations"],
    summary="Pause delivery; new events are held (operators)",
)
def pause(
    integration_id: uuid.UUID, context: Context = Depends(require(Permission.INTEGRATION_OPERATE))
) -> dict[str, Any]:
    return _change_status(integration_id, "pause", context)


@router.post(
    "/integrations/{integration_id}/resume",
    response_model=schemas.StatusChangeResult,
    tags=["integrations"],
    summary="Resume delivery and release held events (operators)",
)
def resume(
    integration_id: uuid.UUID, context: Context = Depends(require(Permission.INTEGRATION_OPERATE))
) -> dict[str, Any]:
    return _change_status(integration_id, "resume", context)
