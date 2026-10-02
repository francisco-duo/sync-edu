from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["infra"])


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """200 se a API e o banco respondem; 503 se o banco está indisponível."""
    engine = request.app.state.engine
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:  # qualquer falha de conexão significa "banco indisponível"
        return JSONResponse(status_code=503, content={"status": "unavailable", "db": "error"})
    return JSONResponse(status_code=200, content={"status": "ok", "db": "ok"})
