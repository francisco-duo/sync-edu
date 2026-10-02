from fastapi.testclient import TestClient

from sync_service.config import Settings
from sync_service.main import create_app

# Porta 1 em localhost: conexão recusada na hora, sem depender de nenhum banco.
UNREACHABLE_DB = "postgresql+asyncpg://user:pass@127.0.0.1:1/none"


def test_health_devolve_503_quando_o_banco_esta_indisponivel() -> None:
    app = create_app(Settings(database_url=UNREACHABLE_DB))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "db": "error"}
