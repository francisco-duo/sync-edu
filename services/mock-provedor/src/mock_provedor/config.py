from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Comportamento caótico inicial, por variáveis de ambiente (alterável em runtime)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    chaos_error_rate: float = 0.0  # chance de responder 5xx ANTES de processar
    chaos_lose_response_rate: float = 0.0  # chance de processar e responder 503 (resposta perdida)
    chaos_retry_after_seconds: int = 1  # Retry-After enviado nos 429
    chaos_seed: int | None = None
