from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="mock-provedor", version="0.1.0")

    @app.get("/health", tags=["infra"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
