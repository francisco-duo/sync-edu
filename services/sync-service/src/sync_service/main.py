from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from sync_service.api import health
from sync_service.config import Settings, get_settings
from sync_service.db.session import create_engine, create_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        yield
        await engine.dispose()

    app = FastAPI(title="sync-service", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    return app
