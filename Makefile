# Atalhos opcionais (Linux/macOS/WSL/Git Bash). No PowerShell use scripts/dev.ps1.
.PHONY: install up down logs test test-integration lint format

install:
	python -m pip install -r requirements-dev.txt

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f

test:
	pytest

test-integration:
	pytest -m integration

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .
