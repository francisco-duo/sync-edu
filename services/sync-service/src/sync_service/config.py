from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração lida de variáveis de ambiente. Nada sensível tem valor padrão."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    academico_base_url: str = "http://mock-academico:8000"
    provedor_base_url: str = "http://mock-provedor:8000"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # database_url vem do ambiente
