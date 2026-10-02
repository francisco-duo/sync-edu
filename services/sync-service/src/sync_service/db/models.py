"""Modelos ORM (só persistência). Veja docs/ESPECIFICACAO.md, seção 8."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from sync_service.db.base import Base
from sync_service.domain.models import ActionType, EntityType
from sync_service.sync.status import ActionStatus, RunStatus, RunTrigger


def _in(column: str, values: type[Any]) -> str:
    quoted = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({quoted})"


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint(_in("status", RunStatus), name="status_valid"),
        CheckConstraint(_in("trigger", RunTrigger), name="trigger_valid"),
        CheckConstraint(
            "total_actions >= 0 AND successful_actions >= 0"
            " AND failed_actions >= 0 AND skipped_actions >= 0",
            name="counters_non_negative",
        ),
        Index("ix_sync_runs_started_at", text("started_at DESC")),
        # No máximo um run REAL em execução por vez. É o lock de execução, guardado no banco.
        Index(
            "ux_sync_runs_single_running",
            text("(true)"),
            unique=True,
            postgresql_where=text("status = 'running' AND dry_run = false"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(16))
    trigger: Mapped[str] = mapped_column(String(16))
    dry_run: Mapped[bool] = mapped_column(Boolean)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_actions: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    successful_actions: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_actions: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    skipped_actions: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    counters: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict, server_default="{}")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class SyncAction(Base):
    __tablename__ = "sync_actions"
    __table_args__ = (
        CheckConstraint(_in("action_type", ActionType), name="action_type_valid"),
        CheckConstraint(_in("entity_type", EntityType), name="entity_type_valid"),
        CheckConstraint(_in("status", ActionStatus), name="status_valid"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        UniqueConstraint("run_id", "action_key", name="uq_sync_actions_run_id_action_key"),
        UniqueConstraint("run_id", "seq", name="uq_sync_actions_run_id_seq"),
        Index("ix_sync_actions_run_id_status", "run_id", "status"),
        Index("ix_sync_actions_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sync_runs.id", ondelete="CASCADE", name="fk_sync_actions_run_id_sync_runs"),
    )
    seq: Mapped[int] = mapped_column(Integer)
    action_key: Mapped[str] = mapped_column(String(200))
    action_type: Mapped[str] = mapped_column(String(24))
    entity_type: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    depends_on: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str | None] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IdMapping(Base):
    __tablename__ = "id_mappings"
    __table_args__ = (
        CheckConstraint("entity_type IN ('user', 'class')", name="entity_type_valid"),
        UniqueConstraint("entity_type", "source_id", name="uq_id_mappings_entity_type_source_id"),
        UniqueConstraint(
            "entity_type", "provider_id", name="uq_id_mappings_entity_type_provider_id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[str] = mapped_column(String(64))
    provider_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
