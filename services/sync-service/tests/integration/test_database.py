from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from sync_service.config import Settings
from sync_service.db.session import create_engine
from sync_service.main import create_app

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def test_conecta_no_postgresql_real(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version()"))
            version = result.scalar_one()
    finally:
        await engine.dispose()

    assert "PostgreSQL" in version


def test_health_devolve_200_com_banco_real(database_url: str) -> None:
    app = create_app(Settings(database_url=database_url))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_alembic_upgrade_head_funciona_sem_revisoes(database_url: str) -> None:
    # Teste síncrono de propósito: o env.py do Alembic usa asyncio.run().
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head")
