from fastapi import FastAPI

from mock_academico.config import Settings
from mock_academico.errors import install_error_handlers
from mock_academico.routes import admin_router, router
from mock_academico.stats import Stats, StatsMiddleware
from mock_academico.store import generate


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="mock-academico", version="0.1.0")
    app.state.settings = settings
    app.state.store = generate(settings.seed_students, settings.seed_classes, settings.seed_random)

    app.state.stats = Stats()

    install_error_handlers(app)
    app.add_middleware(StatsMiddleware)
    app.include_router(router)
    app.include_router(admin_router)

    @app.get("/health", tags=["infra"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
