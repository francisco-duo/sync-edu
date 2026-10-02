# edu-sync — Sincronizador Acadêmico

Projeto de portfólio (totalmente fictício): mantém um sistema acadêmico sincronizado com um
provedor externo de contas e turmas por **reconciliação** (estado desejado × estado atual).

> Status: reconciliação, execução resiliente (retry/backoff, falha parcial), dry-run,
> reprocessamento (`retry-failed`) e auditoria no PostgreSQL implementados.
> Ainda faltam: seed de escala (4.000 alunos), logs JSON, benchmark e agendador.
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

## Usando a API

```bash
curl -X POST "localhost:8000/sync-runs?dry_run=true"    # só calcula e devolve o plano (200)
curl -X POST "localhost:8000/sync-runs"                 # executa em segundo plano (202)
curl localhost:8000/sync-runs/<id>                      # status e contadores
curl "localhost:8000/sync-runs/<id>/actions?status=failed"
curl -X POST localhost:8000/sync-runs/<id>/retry-failed # reprocessa só o que falhou
```

Swagger em <http://localhost:8000/docs>. Para ver a resiliência, derrube/atrapalhe o provedor:

```bash
curl -X PUT localhost:8002/_admin/chaos -H 'content-type: application/json' \
     -d '{"error_rate":0.25,"lose_response_rate":0.1,"retry_after_seconds":0,"seed":3}'
curl -X POST localhost:8000/sync-runs      # termina com retries; a sync seguinte tem 0 ações
```

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
