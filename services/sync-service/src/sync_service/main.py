import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from sync_service.api import health, sync_runs
from sync_service.clients.academico import AcademicoClient
from sync_service.config import Settings, get_settings
from sync_service.db.session import create_engine, create_session_factory
from sync_service.errors import install_error_handlers
from sync_service.providers.mock_adapter import MockProvedorAdapter
from sync_service.resilience.rate_limit import AsyncRateLimiter
from sync_service.sync import store
from sync_service.sync.executor import ActionExecutor, ExecutionConfig
from sync_service.sync.runner import SyncRunner
from sync_service.telemetry import Telemetry

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=5.0)


def create_app(
    settings: Settings | None = None,
    *,
    academico_transport: httpx.AsyncBaseTransport | None = None,
    provedor_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """`*_transport` permitem apontar os clientes para apps em processo (testes) em vez da rede."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        sessions = create_session_factory(engine)
        telemetry = Telemetry()
        provedor_hooks = [telemetry.request_hook("provedor")]
        if settings.provider_max_rps > 0:
            # Respeita o limite do provedor antes de enviar (e antes de cada retry).
            provedor_hooks.insert(0, AsyncRateLimiter(settings.provider_max_rps).request_hook)
        academico_http = httpx.AsyncClient(
            base_url=settings.academico_base_url,
            timeout=HTTP_TIMEOUT,
            transport=academico_transport,
            event_hooks={"request": [telemetry.request_hook("academico")]},
        )
        provedor_http = httpx.AsyncClient(
            base_url=settings.provedor_base_url,
            timeout=HTTP_TIMEOUT,
            transport=provedor_transport,
            event_hooks={"request": provedor_hooks},
        )
        policy = settings.retry_policy()
        academico = AcademicoClient(
            academico_http,
            retry_policy=policy,
            concurrency=settings.snapshot_concurrency,
            on_retry=telemetry.retry_hook("academico"),
        )
        provider = MockProvedorAdapter(
            provedor_http,
            retry_policy=policy,
            concurrency=settings.snapshot_concurrency,
            on_retry=telemetry.retry_hook("provedor"),
        )
        executor = ActionExecutor(
            provider,
            ExecutionConfig(
                retry_policy=policy,
                concurrency=settings.provider_concurrency,
                batch_size=settings.batch_size,
                max_consecutive_failures=settings.max_consecutive_failures,
            ),
            on_retry=telemetry.retry_hook("provedor"),
        )
        runner = SyncRunner(
            sessions,
            academico,
            provider,
            executor,
            email_domain=settings.email_domain,
            telemetry=telemetry,
        )
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = sessions
        app.state.runner = runner

        await _mark_interrupted(sessions)
        yield
        await runner.shutdown()
        await academico_http.aclose()
        await provedor_http.aclose()
        await engine.dispose()

    app = FastAPI(title="sync-service", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(sync_runs.router)
    return app


async def _mark_interrupted(sessions: store.SessionFactory) -> None:
    """Se o banco ainda não está pronto, o /health acusa; não derrubamos o startup por isso."""
    try:
        await store.mark_interrupted_runs(sessions)
    except Exception:  # noqa: BLE001 (tabela ainda inexistente, banco fora do ar...)
        logger.warning("não foi possível marcar runs interrompidos no startup", exc_info=True)
