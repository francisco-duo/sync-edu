FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1     PIP_NO_CACHE_DIR=1     PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

RUN useradd --system --uid 10001 app
USER app

EXPOSE 8000
CMD ["uvicorn", "mock_provedor.main:app", "--host", "0.0.0.0", "--port", "8000"]
