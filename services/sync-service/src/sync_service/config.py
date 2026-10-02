from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from sync_service.resilience.retry import RetryPolicy


class Settings(BaseSettings):
    """Configuração lida de variáveis de ambiente. Nada sensível tem valor padrão."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    academico_base_url: str = "http://mock-academico:8000"
    provedor_base_url: str = "http://mock-provedor:8000"
    log_level: str = "INFO"
    email_domain: str = "example.edu"

    # Retry (veja docs/ESPECIFICACAO.md, seção 7)
    retry_max_attempts: int = Field(default=5, ge=1)
    retry_base_delay_seconds: float = Field(default=0.5, ge=0)
    retry_factor: float = Field(default=2.0, ge=1)
    retry_max_delay_seconds: float = Field(default=30.0, ge=0)
    retry_jitter: Literal["full", "none"] = "full"
    retry_max_elapsed_seconds: float = Field(default=120.0, gt=0)

    # Concorrência e execução
    snapshot_concurrency: int = Field(default=5, ge=1)
    provider_concurrency: int = Field(default=10, ge=1)
    provider_max_rps: float = Field(default=0.0, ge=0)  # 0 = sem limite de taxa no cliente
    batch_size: int = Field(default=200, ge=1)
    max_consecutive_failures: int = Field(default=25, ge=1)

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=self.retry_max_attempts,
            base_delay=self.retry_base_delay_seconds,
            factor=self.retry_factor,
            max_delay=self.retry_max_delay_seconds,
            jitter=self.retry_jitter,
            max_elapsed=self.retry_max_elapsed_seconds,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # database_url vem do ambiente
