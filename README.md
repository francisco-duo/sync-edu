# edu-sync — Sincronizador Acadêmico

Projeto de portfólio (totalmente fictício): mantém um sistema acadêmico sincronizado com um
provedor externo de contas e turmas por **reconciliação** (estado desejado × estado atual).

> Status: **fundação** pronta (monorepo, Docker, PostgreSQL, Alembic, CI).
> O algoritmo de sincronização ainda não foi implementado.
> A especificação completa está em [docs/ESPECIFICACAO.md](docs/ESPECIFICACAO.md).

## Serviços

| Serviço | Porta (host) | Papel |
|---|---|---|
| `sync-service` | 8000 | O produto: reconcilia, planeja, executa e audita |
| `mock-academico` | 8001 | Fonte da verdade fictícia |
| `mock-provedor` | 8002 | Provedor externo fictício |
| `postgres` | 5433 | Banco do `sync-service` |

Dentro do Docker os serviços se falam pelo nome (`http://mock-academico:8000`, `postgres:5432`).

## Como executar

```bash
cp .env.example .env        # troque POSTGRES_PASSWORD
docker compose up --build   # sobe tudo
docker compose down         # derruba (use -v para apagar o banco)
```

Verificação rápida: <http://localhost:8000/health> (devolve `{"status":"ok","db":"ok"}`).
Swagger de cada serviço em `/docs`.

## Desenvolvimento local

Requer Python 3.12+. Evite criar o `.venv` dentro de uma pasta sincronizada pelo OneDrive.

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

pytest                    # testes unitários (não precisam de banco)
pytest -m integration     # integração: precisa do PostgreSQL (docker compose up -d postgres)
ruff check .              # lint
ruff format .             # formatação
```

Atalhos: `make <tarefa>` (Linux/macOS/WSL) ou `.\scripts\dev.ps1 <tarefa>` (PowerShell);
tarefas: `install`, `up`, `down`, `logs`, `test`, `test-integration`, `lint`, `format`.

Os testes de integração leem `TEST_DATABASE_URL` (veja `.env.example`).
