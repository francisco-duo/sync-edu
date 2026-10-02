from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from sync_service.db.session import create_engine
from sync_testkit.world import World, reset_database

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


class _IntegrationEnv(BaseSettings):
    """Lê TEST_DATABASE_URL do ambiente ou do .env. Falha alto se não existir."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    test_database_url: str


@pytest.fixture(scope="session")
def database_url() -> str:
    return _IntegrationEnv().test_database_url  # type: ignore[call-arg]


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """Aplica as migrations uma vez por sessão (síncrono: o env.py do Alembic usa asyncio.run)."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    return database_url


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(migrated_database)
    await reset_database(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
async def world(engine: AsyncEngine) -> AsyncIterator[World]:
    world = World(engine)
    yield world
    await world.aclose()
