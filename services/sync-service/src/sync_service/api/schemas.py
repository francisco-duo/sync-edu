import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    seq: int
    action_key: str
    action_type: str
    entity_type: str
    entity_id: str
    payload: dict[str, Any]
    depends_on: list[str]
    status: str
    outcome: str | None
    attempts: int
    error_code: str | None
    last_error: str | None
    last_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    dry_run: bool
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    total_actions: int
    successful_actions: int
    failed_actions: int
    skipped_actions: int
    counters: dict[str, int]
    metrics: dict[str, Any]
    retry_count: int
    last_retry_at: datetime | None
    error: str | None


class RunWithActions(RunSummary):
    actions: list[ActionOut]


class RunPage(BaseModel):
    items: list[RunSummary]
    page: int
    page_size: int
    total: int


class ActionPage(BaseModel):
    items: list[ActionOut]
    page: int
    page_size: int
    total: int
