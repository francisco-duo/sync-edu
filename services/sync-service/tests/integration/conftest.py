import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict


class _IntegrationEnv(BaseSettings):
    """Lê TEST_DATABASE_URL do ambiente ou do .env. Falha alto se não existir."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    test_database_url: str


@pytest.fixture(scope="session")
def database_url() -> str:
    return _IntegrationEnv().test_database_url  # type: ignore[call-arg]
