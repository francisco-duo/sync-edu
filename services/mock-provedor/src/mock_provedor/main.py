from fastapi import FastAPI

from mock_provedor.chaos import ChaosState, Stats, chaos_middleware
from mock_provedor.config import Settings
from mock_provedor.errors import install_error_handlers
from mock_provedor.routes import admin_router, classes_router, users_router
from mock_provedor.store import Store


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="mock-provedor", version="0.1.0")

    app.state.store = Store()
    app.state.stats = Stats()
    chaos = ChaosState()
    chaos.configure(
        error_rate=settings.chaos_error_rate,
        lose_response_rate=settings.chaos_lose_response_rate,
        retry_after_seconds=settings.chaos_retry_after_seconds,
        seed=settings.chaos_seed,
        fail_next=[],
        lose_next=0,
    )
    app.state.chaos = chaos

    install_error_handlers(app)
    app.middleware("http")(chaos_middleware)
    app.include_router(users_router)
    app.include_router(classes_router)
    app.include_router(admin_router)

    @app.get("/health", tags=["infra"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
