from fastapi.testclient import TestClient

from mock_academico.main import create_app


def test_health_responde_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
