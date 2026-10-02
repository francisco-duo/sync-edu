"""tabelas iniciais: sync_runs, sync_actions, id_mappings

Revision ID: 0001
Revises:
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "sync_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_actions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("successful_actions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_actions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_actions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("counters", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("metrics", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sync_runs"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'aborted', 'failed',"
            " 'interrupted', 'planned')",
            name="ck_sync_runs_status_valid",
        ),
        sa.CheckConstraint("trigger IN ('manual', 'scheduled')", name="ck_sync_runs_trigger_valid"),
        sa.CheckConstraint(
            "total_actions >= 0 AND successful_actions >= 0"
            " AND failed_actions >= 0 AND skipped_actions >= 0",
            name="ck_sync_runs_counters_non_negative",
        ),
    )
    op.create_index("ix_sync_runs_started_at", "sync_runs", [sa.text("started_at DESC")])
    op.create_index(
        "ux_sync_runs_single_running",
        "sync_runs",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("status = 'running' AND dry_run = false"),
    )

    op.create_table(
        "sync_actions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("action_key", sa.String(200), nullable=False),
        sa.Column("action_type", sa.String(24), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("depends_on", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_sync_actions"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["sync_runs.id"],
            name="fk_sync_actions_run_id_sync_runs",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("run_id", "action_key", name="uq_sync_actions_run_id_action_key"),
        sa.UniqueConstraint("run_id", "seq", name="uq_sync_actions_run_id_seq"),
        sa.CheckConstraint(
            "action_type IN ('CREATE_CLASS', 'CREATE_USER', 'REACTIVATE_USER',"
            " 'REMOVE_FROM_CLASS', 'ADD_TO_CLASS', 'SUSPEND_USER')",
            name="ck_sync_actions_action_type_valid",
        ),
        sa.CheckConstraint(
            "entity_type IN ('user', 'class', 'membership')",
            name="ck_sync_actions_entity_type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'pending', 'succeeded', 'failed', 'skipped')",
            name="ck_sync_actions_status_valid",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_sync_actions_attempts_non_negative"),
    )
    op.create_index("ix_sync_actions_run_id_status", "sync_actions", ["run_id", "status"])
    op.create_index("ix_sync_actions_entity", "sync_actions", ["entity_type", "entity_id"])

    op.create_table(
        "id_mappings",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_id_mappings"),
        sa.UniqueConstraint(
            "entity_type", "source_id", name="uq_id_mappings_entity_type_source_id"
        ),
        sa.UniqueConstraint(
            "entity_type", "provider_id", name="uq_id_mappings_entity_type_provider_id"
        ),
        sa.CheckConstraint(
            "entity_type IN ('user', 'class')", name="ck_id_mappings_entity_type_valid"
        ),
    )


def downgrade() -> None:
    op.drop_table("id_mappings")
    op.drop_table("sync_actions")
    op.drop_table("sync_runs")
