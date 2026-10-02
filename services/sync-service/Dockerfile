FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# O Alembic roda a partir de /app (alembic.ini e alembic/ ficam fora do pacote).
COPY alembic.ini ./
COPY alembic ./alembic

RUN useradd --system --uid 10001 app
USER app

EXPOSE 8000
# Aplica as migrations e sobe a API (aceitável enquanto houver uma única instância).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn sync_service.main:create_app --factory --host 0.0.0.0 --port 8000"]
