"""initial schema

Revision ID: 0001
Revises: (none)
Create Date: 2026-10-11 02:26:26.771550
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table('local_queue_messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('topic', sa.String(length=120), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('attributes', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='ready', nullable=False),
    sa.Column('delivery_attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('ready', 'dead')", name=op.f('ck_local_queue_messages_status_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_local_queue_messages'))
    )
    op.create_index('ix_local_queue_ready', 'local_queue_messages', ['available_at'], unique=False, postgresql_where=sa.text("status = 'ready'"))
    op.create_table('rate_limit_counters',
    sa.Column('scope_key', sa.String(length=200), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.PrimaryKeyConstraint('scope_key', 'window_start', name=op.f('pk_rate_limit_counters'))
    )
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('email = lower(email)', name=op.f('ck_users_email_lowercase')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email'))
    )
    op.create_table('workspaces',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('slug', sa.String(length=80), nullable=False),
    sa.Column('is_demo', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workspaces')),
    sa.UniqueConstraint('slug', name=op.f('uq_workspaces_slug'))
    )
    op.create_table('audit_logs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=True),
    sa.Column('actor_type', sa.String(length=20), nullable=False),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('action', sa.String(length=80), nullable=False),
    sa.Column('entity_type', sa.String(length=40), nullable=False),
    sa.Column('entity_id', sa.String(length=80), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('correlation_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_audit_logs_actor_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_audit_logs_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_logs'))
    )
    op.create_index('ix_audit_entity', 'audit_logs', ['workspace_id', 'entity_type', 'entity_id'], unique=False)
    op.create_index('ix_audit_ws_created', 'audit_logs', ['workspace_id', sa.literal_column('created_at DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_table('connections',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('direction', sa.String(length=20), nullable=False),
    sa.Column('adapter_type', sa.String(length=40), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('secret_ref', sa.String(length=200), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("direction IN ('source', 'destination')", name=op.f('ck_connections_direction_valid')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_connections_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_connections_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_connections')),
    sa.UniqueConstraint('workspace_id', 'name', name=op.f('uq_connections_workspace_id'))
    )
    op.create_table('demo_destination_requests',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('attempts_seen', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('accepted_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_demo_destination_requests_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_demo_destination_requests')),
    sa.UniqueConstraint('workspace_id', 'idempotency_key', name='uq_demo_requests_key')
    )
    op.create_table('demo_destination_settings',
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('mode', sa.String(length=24), server_default='healthy', nullable=False),
    sa.Column('fail_first_n', sa.Integer(), server_default=sa.text('2'), nullable=False),
    sa.Column('delay_seconds', sa.Double(), server_default=sa.text('3'), nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_demo_destination_settings_updated_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_demo_destination_settings_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('workspace_id', name=op.f('pk_demo_destination_settings'))
    )
    op.create_table('sessions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('csrf_token', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('user_agent', sa.String(length=300), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_sessions_token_hash'))
    )
    op.create_index(op.f('ix_sessions_user_id'), 'sessions', ['user_id'], unique=False)
    op.create_table('workspace_memberships',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('admin', 'operator', 'viewer')", name=op.f('ck_workspace_memberships_role_valid')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_workspace_memberships_user_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_workspace_memberships_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workspace_memberships')),
    sa.UniqueConstraint('workspace_id', 'user_id', name=op.f('uq_workspace_memberships_workspace_id'))
    )
    op.create_index(op.f('ix_workspace_memberships_user_id'), 'workspace_memberships', ['user_id'], unique=False)
    op.create_table('integrations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('source_connection_id', sa.UUID(), nullable=False),
    sa.Column('destination_connection_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='draft', nullable=False),
    sa.Column('current_version_id', sa.UUID(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'active', 'paused', 'disabled')", name=op.f('ck_integrations_status_valid')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_integrations_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['destination_connection_id'], ['connections.id'], name=op.f('fk_integrations_destination_connection_id_connections'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_connection_id'], ['connections.id'], name=op.f('fk_integrations_source_connection_id_connections'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_integrations_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_integrations')),
    sa.UniqueConstraint('workspace_id', 'name', name=op.f('uq_integrations_workspace_id'))
    )
    op.create_index('ix_integrations_destination_connection_id', 'integrations', ['destination_connection_id'], unique=False)
    op.create_index('ix_integrations_source_routing', 'integrations', ['source_connection_id', 'status'], unique=False)
    op.create_table('integration_versions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('integration_id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('change_note', sa.Text(), server_default='', nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_integration_versions_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['integration_id'], ['integrations.id'], name=op.f('fk_integration_versions_integration_id_integrations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_integration_versions_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_integration_versions')),
    sa.UniqueConstraint('integration_id', 'version', name=op.f('uq_integration_versions_integration_id'))
    )
    # integrations <-> integration_versions reference each other, so this
    # constraint can only be added once both tables exist.
    op.create_foreign_key(
        op.f('fk_integrations_current_version_id_integration_versions'),
        'integrations', 'integration_versions', ['current_version_id'], ['id'], ondelete='RESTRICT',
    )
    op.create_table('events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('integration_id', sa.UUID(), nullable=False),
    sa.Column('integration_version_id', sa.UUID(), nullable=False),
    sa.Column('source_connection_id', sa.UUID(), nullable=False),
    sa.Column('source_event_id', sa.String(length=255), nullable=True),
    sa.Column('dedup_key', sa.String(length=80), nullable=False),
    sa.Column('event_type', sa.String(length=128), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('payload_sha256', sa.String(length=64), nullable=False),
    sa.Column('payload_size_bytes', sa.Integer(), nullable=False),
    sa.Column('payload_purged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('transformed_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('correlation_id', sa.String(length=64), nullable=False),
    sa.Column('delivery_cycle', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('attempt_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processing_lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failure_stage', sa.String(length=32), nullable=True),
    sa.Column('failure_category', sa.String(length=40), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('failure_details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dead_lettered_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('RECEIVED', 'HELD', 'VALIDATION_FAILED', 'FILTERED', 'PENDING_PUBLISH', 'QUEUED', 'PROCESSING', 'RETRY_SCHEDULED', 'DELIVERED', 'DEAD_LETTERED', 'REPLAY_REQUESTED')", name=op.f('ck_events_status_valid')),
    sa.CheckConstraint('attempt_count >= 0 AND delivery_cycle >= 1', name=op.f('ck_events_counters_valid')),
    sa.ForeignKeyConstraint(['integration_id'], ['integrations.id'], name=op.f('fk_events_integration_id_integrations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['integration_version_id'], ['integration_versions.id'], name=op.f('fk_events_integration_version_id_integration_versions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_connection_id'], ['connections.id'], name=op.f('fk_events_source_connection_id_connections'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_events_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_events')),
    sa.UniqueConstraint('integration_id', 'dedup_key', name='uq_events_integration_dedup')
    )
    op.create_index('ix_events_correlation_id', 'events', ['correlation_id'], unique=False)
    op.create_index('ix_events_held', 'events', ['integration_id'], unique=False, postgresql_where=sa.text("status = 'HELD'"))
    op.create_index('ix_events_processing_lease', 'events', ['processing_lease_expires_at'], unique=False, postgresql_where=sa.text("status = 'PROCESSING'"))
    op.create_index('ix_events_ws_integration_received', 'events', ['workspace_id', 'integration_id', sa.literal_column('received_at DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_index('ix_events_ws_received', 'events', ['workspace_id', sa.literal_column('received_at DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_index('ix_events_ws_source_event_id', 'events', ['workspace_id', 'source_event_id'], unique=False)
    op.create_index('ix_events_ws_status_received', 'events', ['workspace_id', 'status', sa.literal_column('received_at DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_table('event_transitions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('event_id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('from_status', sa.String(length=24), nullable=True),
    sa.Column('to_status', sa.String(length=24), nullable=False),
    sa.Column('reason', sa.String(length=80), nullable=False),
    sa.Column('actor_type', sa.String(length=20), nullable=False),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_event_transitions_actor_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_event_transitions_event_id_events'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_event_transitions_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_event_transitions'))
    )
    op.create_index('ix_event_transitions_event', 'event_transitions', ['event_id', 'id'], unique=False)
    op.create_table('investigations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('event_id', sa.UUID(), nullable=False),
    sa.Column('requested_by', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('provider', sa.String(length=40), nullable=True),
    sa.Column('model', sa.String(length=120), nullable=True),
    sa.Column('report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('evidence_refs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('trace', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('decisions', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('fallback_reason', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("mode IN ('ai', 'deterministic')", name=op.f('ck_investigations_mode_valid')),
    sa.CheckConstraint("status IN ('completed', 'failed')", name=op.f('ck_investigations_status_valid')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_investigations_event_id_events'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name=op.f('fk_investigations_requested_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_investigations_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_investigations'))
    )
    op.create_index(op.f('ix_investigations_event_id'), 'investigations', ['event_id'], unique=False)
    op.create_index('ix_investigations_ws_created', 'investigations', ['workspace_id', sa.literal_column('created_at DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_table('outbox_messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('event_id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('cycle', sa.Integer(), nullable=False),
    sa.Column('attempt_number', sa.Integer(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='pending', nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('claim_token', sa.UUID(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('publish_attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('broker_message_id', sa.String(length=255), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'published', 'cancelled')", name=op.f('ck_outbox_messages_status_valid')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_outbox_messages_event_id_events'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_outbox_messages_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_outbox_messages')),
    sa.UniqueConstraint('event_id', 'cycle', 'attempt_number', name='uq_outbox_event_attempt')
    )
    op.create_index('ix_outbox_pending_due', 'outbox_messages', ['available_at', 'id'], unique=False, postgresql_where=sa.text("status = 'pending'"))
    op.create_table('replay_operations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('event_id', sa.UUID(), nullable=False),
    sa.Column('requested_by', sa.UUID(), nullable=True),
    sa.Column('reason', sa.Text(), server_default='', nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('version_mode', sa.String(length=16), nullable=False),
    sa.Column('integration_version_id', sa.UUID(), nullable=False),
    sa.Column('from_status', sa.String(length=24), nullable=False),
    sa.Column('cycle', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("version_mode IN ('original', 'current')", name=op.f('ck_replay_operations_version_mode_valid')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_replay_operations_event_id_events'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['integration_version_id'], ['integration_versions.id'], name=op.f('fk_replay_operations_integration_version_id_integration_versions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name=op.f('fk_replay_operations_requested_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_replay_operations_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_replay_operations')),
    sa.UniqueConstraint('workspace_id', 'idempotency_key', name='uq_replay_idempotency')
    )
    op.create_index(op.f('ix_replay_operations_event_id'), 'replay_operations', ['event_id'], unique=False)
    op.create_table('delivery_attempts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('event_id', sa.UUID(), nullable=False),
    sa.Column('workspace_id', sa.UUID(), nullable=False),
    sa.Column('integration_version_id', sa.UUID(), nullable=False),
    sa.Column('replay_operation_id', sa.UUID(), nullable=True),
    sa.Column('cycle', sa.Integer(), nullable=False),
    sa.Column('attempt_number', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='in_progress', nullable=False),
    sa.Column('disposition', sa.String(length=16), nullable=True),
    sa.Column('failure_category', sa.String(length=40), nullable=True),
    sa.Column('http_status', sa.Integer(), nullable=True),
    sa.Column('request_method', sa.String(length=10), nullable=True),
    sa.Column('request_url', sa.Text(), nullable=True),
    sa.Column('response_summary', sa.Text(), nullable=True),
    sa.Column('retry_after_seconds', sa.Double(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=80), nullable=False),
    sa.Column('correlation_id', sa.String(length=64), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.CheckConstraint("status IN ('in_progress', 'succeeded', 'failed')", name=op.f('ck_delivery_attempts_status_valid')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_delivery_attempts_event_id_events'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['integration_version_id'], ['integration_versions.id'], name=op.f('fk_delivery_attempts_integration_version_id_integration_versions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['replay_operation_id'], ['replay_operations.id'], name=op.f('fk_delivery_attempts_replay_operation_id_replay_operations'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_delivery_attempts_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_delivery_attempts')),
    sa.UniqueConstraint('event_id', 'cycle', 'attempt_number', name='uq_attempts_event_attempt')
    )
    op.create_index('ix_attempts_ws_started', 'delivery_attempts', ['workspace_id', sa.literal_column('started_at DESC')], unique=False)


def downgrade() -> None:
    op.drop_index('ix_attempts_ws_started', table_name='delivery_attempts')
    op.drop_table('delivery_attempts')
    op.drop_index(op.f('ix_replay_operations_event_id'), table_name='replay_operations')
    op.drop_table('replay_operations')
    op.drop_index('ix_outbox_pending_due', table_name='outbox_messages', postgresql_where=sa.text("status = 'pending'"))
    op.drop_table('outbox_messages')
    op.drop_index('ix_investigations_ws_created', table_name='investigations')
    op.drop_index(op.f('ix_investigations_event_id'), table_name='investigations')
    op.drop_table('investigations')
    op.drop_index('ix_event_transitions_event', table_name='event_transitions')
    op.drop_table('event_transitions')
    op.drop_index('ix_events_ws_status_received', table_name='events')
    op.drop_index('ix_events_ws_source_event_id', table_name='events')
    op.drop_index('ix_events_ws_received', table_name='events')
    op.drop_index('ix_events_ws_integration_received', table_name='events')
    op.drop_index('ix_events_processing_lease', table_name='events', postgresql_where=sa.text("status = 'PROCESSING'"))
    op.drop_index('ix_events_held', table_name='events', postgresql_where=sa.text("status = 'HELD'"))
    op.drop_index('ix_events_correlation_id', table_name='events')
    op.drop_table('events')
    op.drop_constraint(
        op.f('fk_integrations_current_version_id_integration_versions'), 'integrations', type_='foreignkey'
    )
    op.drop_table('integration_versions')
    op.drop_index('ix_integrations_source_routing', table_name='integrations')
    op.drop_index('ix_integrations_destination_connection_id', table_name='integrations')
    op.drop_table('integrations')
    op.drop_index(op.f('ix_workspace_memberships_user_id'), table_name='workspace_memberships')
    op.drop_table('workspace_memberships')
    op.drop_index(op.f('ix_sessions_user_id'), table_name='sessions')
    op.drop_table('sessions')
    op.drop_table('demo_destination_settings')
    op.drop_table('demo_destination_requests')
    op.drop_table('connections')
    op.drop_index('ix_audit_ws_created', table_name='audit_logs')
    op.drop_index('ix_audit_entity', table_name='audit_logs')
    op.drop_table('audit_logs')
    op.drop_table('workspaces')
    op.drop_table('users')
    op.drop_table('rate_limit_counters')
    op.drop_index('ix_local_queue_ready', table_name='local_queue_messages', postgresql_where=sa.text("status = 'ready'"))
    op.drop_table('local_queue_messages')
