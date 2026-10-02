# edu-sync — Sincronizador Acadêmico

Projeto de portfólio (totalmente fictício): mantém um sistema acadêmico sincronizado com um
provedor externo de contas e turmas por **reconciliação** (estado desejado × estado atual).

> Status: reconciliação, execução resiliente (retry/backoff, limite de taxa, falha parcial),
> dry-run, reprocessamento (`retry-failed`), auditoria no PostgreSQL e benchmark em escala
> implementados. Ainda faltam: logs JSON e o agendador diário.
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

## Benchmark (números medidos)

Escala de demonstração, gerada com Faker (`seed=42`), rodando em Docker na minha máquina
(mesma máquina para todos os serviços; provedor = mock em memória). Detalhes, método, tabelas
completas e limitações em [docs/benchmark.md](docs/benchmark.md).

```text
Dataset:
4.000 alunos
90 turmas
4.000 matrículas (ativas, uma por aluno)

Abertura (8.090 ações):            20,6 a 22,3 s   (3 execuções)
Sem alterações (0 ações):          0,44 a 0,53 s   (3 execuções)
Alterações (805 ações):            2,30 a 2,43 s   (3 execuções)
```

Alterações do cenário C: 100 alunos novos, 100 que saíram, 200 que trocaram de turma e 5 turmas
novas. Nos três cenários o provedor terminou idêntico ao acadêmico.

**Não é perfeito, e o motivo está explicado:** a abertura é limitada pela biblioteca HTTP do
cliente (httpx: ~900 req/s no máximo, e piora com muita simultaneidade), não pelo banco nem pelo
algoritmo. Com um provedor de latência maior (20 ms) a concorrência de 10 é a melhor medida;
contra o mock local, 3 a 5 simultâneas levam 35-37% menos tempo (14,1-14,6 s). Com 5% de erros 5xx e 2% de
respostas perdidas injetados, a abertura levou 43 s, com 571 retries, **0 falhas** e a sync
seguinte com 0 ações. Rodar de novo: `python scripts/benchmark.py` (com a stack no ar).

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
