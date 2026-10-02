from enum import StrEnum


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"  # terminou, mas há ações failed/skipped
    ABORTED = "aborted"  # circuit breaker: o provedor parece fora do ar
    FAILED = "failed"  # não conseguiu nem calcular o plano
    INTERRUPTED = "interrupted"  # o processo caiu no meio
    PLANNED = "planned"  # dry-run


class RunTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class ActionStatus(StrEnum):
    PLANNED = "planned"  # só em dry-run
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ErrorCode:
    """Códigos de erro gerados pelo próprio sync (os demais vêm do provedor)."""

    RETRIES_EXHAUSTED = "RETRIES_EXHAUSTED"
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"
    RUN_ABORTED = "RUN_ABORTED"
    MAPPING_NOT_FOUND = "MAPPING_NOT_FOUND"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


# `skipped` por estes motivos pode ser reprocessado; os demais estados finais, não.
RETRYABLE_SKIP_CODES = (ErrorCode.DEPENDENCY_FAILED, ErrorCode.RUN_ABORTED)
