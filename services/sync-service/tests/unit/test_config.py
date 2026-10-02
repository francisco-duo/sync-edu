import pytest
from pydantic import ValidationError

from sync_service.config import Settings


def test_database_url_e_obrigatoria(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_valores_padrao_apontam_para_os_nomes_dos_servicos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@host/db")

    settings = Settings(_env_file=None)

    assert settings.academico_base_url == "http://mock-academico:8000"
    assert settings.provedor_base_url == "http://mock-provedor:8000"
