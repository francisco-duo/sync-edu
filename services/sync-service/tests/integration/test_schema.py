"""Esquema real no PostgreSQL: a migration bate com os modelos e as restrições protegem os dados."""

import uuid
from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from sync_service.db.base import Base

pytestmark = pytest.mark.integration


def _run_status_insert(status: str, dry_run: bool) -> tuple[str, dict]:
    return (
        "INSERT INTO sync_runs (id, status, trigger, dry_run, started_at)"
        " VALUES (:id, :status, 'manual', :dry_run, :now)",
        {"id": uuid.uuid4(), "status": status, "dry_run": dry_run, "now": datetime.now(UTC)},
    )


async def test_migration_e_modelos_estao_sincronizados(engine: AsyncEngine) -> None:
    def diff(connection) -> list:  # noqa: ANN001
        context = MigrationContext.configure(connection)
        return compare_metadata(context, Base.metadata)

    async with engine.connect() as connection:
        differences = await connection.run_sync(diff)

    assert differences == []


async def test_mapeamento_duplicado_e_rejeitado_pelo_banco(engine: AsyncEngine) -> None:
    insert = text(
        "INSERT INTO id_mappings (entity_type, source_id, provider_id) VALUES (:t, :s, :p)"
    )
    async with engine.begin() as connection:
        await connection.execute(insert, {"t": "user", "s": "S1", "p": "usr_1"})

    # mesmo id de origem
    with pytest.raises(IntegrityError, match="uq_id_mappings_entity_type_source_id"):
        async with engine.begin() as connection:
            await connection.execute(insert, {"t": "user", "s": "S1", "p": "usr_2"})
    # mesmo id do provedor
    with pytest.raises(IntegrityError, match="uq_id_mappings_entity_type_provider_id"):
        async with engine.begin() as connection:
            await connection.execute(insert, {"t": "user", "s": "S2", "p": "usr_1"})
    # o mesmo id em outro tipo de entidade é permitido
    async with engine.begin() as connection:
        await connection.execute(insert, {"t": "class", "s": "S1", "p": "usr_1"})


async def test_so_um_run_real_em_execucao_por_vez(engine: AsyncEngine) -> None:
    sql, params = _run_status_insert("running", dry_run=False)
    async with engine.begin() as connection:
        await connection.execute(text(sql), params)

    sql2, params2 = _run_status_insert("running", dry_run=False)
    with pytest.raises(IntegrityError, match="ux_sync_runs_single_running"):
        async with engine.begin() as connection:
            await connection.execute(text(sql2), params2)


async def test_dry_runs_nao_competem_pelo_lock_de_execucao(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        for dry_run in (False, True, True):
            sql, params = _run_status_insert("running", dry_run=dry_run)
            await connection.execute(text(sql), params)


@pytest.mark.parametrize("status", ["done", "RUNNING", ""])
async def test_status_de_run_invalido_e_rejeitado(engine: AsyncEngine, status: str) -> None:
    sql, params = _run_status_insert(status, dry_run=True)

    with pytest.raises(IntegrityError, match="ck_sync_runs_status_valid"):
        async with engine.begin() as connection:
            await connection.execute(text(sql), params)


async def test_acao_duplicada_no_mesmo_run_e_rejeitada_e_a_remocao_do_run_cascateia(
    engine: AsyncEngine,
) -> None:
    sql, params = _run_status_insert("planned", dry_run=True)
    run_id = params["id"]
    action = text(
        "INSERT INTO sync_actions (run_id, seq, action_key, action_type, entity_type, entity_id,"
        " payload, status) VALUES (:run, :seq, 'CREATE_USER:S1', 'CREATE_USER', 'user', 'S1',"
        " '{}', 'planned')"
    )
    async with engine.begin() as connection:
        await connection.execute(text(sql), params)
        await connection.execute(action, {"run": run_id, "seq": 0})

    with pytest.raises(IntegrityError, match="uq_sync_actions_run_id_action_key"):
        async with engine.begin() as connection:
            await connection.execute(action, {"run": run_id, "seq": 1})

    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM sync_runs WHERE id = :id"), {"id": run_id})
        remaining = await connection.scalar(text("SELECT count(*) FROM sync_actions"))
    assert remaining == 0


async def test_acao_nao_pode_apontar_para_run_inexistente(engine: AsyncEngine) -> None:
    with pytest.raises(DBAPIError, match="fk_sync_actions_run_id_sync_runs"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO sync_actions (run_id, seq, action_key, action_type, entity_type,"
                    " entity_id, payload, status) VALUES (:run, 0, 'k', 'CREATE_USER', 'user',"
                    " 'S1', '{}', 'planned')"
                ),
                {"run": uuid.uuid4()},
            )


async def test_nao_existe_tipo_de_acao_de_exclusao_no_banco(engine: AsyncEngine) -> None:
    sql, params = _run_status_insert("planned", dry_run=True)
    async with engine.begin() as connection:
        await connection.execute(text(sql), params)

    async def insert_delete_user() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO sync_actions (run_id, seq, action_key, action_type, entity_type,"
                    " entity_id, payload, status) VALUES (:run, 0, 'k', 'DELETE_USER', 'user',"
                    " 'S1', '{}', 'planned')"
                ),
                {"run": params["id"]},
            )

    with pytest.raises(IntegrityError, match="ck_sync_actions_action_type_valid"):
        await insert_delete_user()
