# Benchmark do edu-sync

Todos os números abaixo foram **medidos** neste repositório (`scripts/benchmark.py`). Nada é
estimativa. Os resultados completos do run de referência estão em
[`benchmark-results.json`](benchmark-results.json).

## Como reproduzir

```bash
docker compose up -d --build --wait
python scripts/benchmark.py --label "padrão" --json-out docs/benchmark-results.json
```

O script reinicia os dois mocks, gera o dataset (Faker, `seed=42`), e roda os cenários abaixo,
medindo o tempo de ponta a ponta, as requisições (contadas pelos próprios mocks, retries
inclusos), os retries (telemetria do sync-service) e se o provedor terminou **igual** ao
acadêmico (contagens de usuários, suspensos, turmas e matrículas).

| Cenário | O que é |
|---|---|
| A0 | dry-run da abertura: só calcula e devolve o plano |
| **A** | **abertura do período**: tudo é criado do zero |
| **B** | **execução diária sem mudanças** (esperado: 0 ações) |
| **C** | **alterações diárias**: 100 alunos novos, 100 que saíram, 200 que trocaram de turma, 5 turmas novas |
| B2 | sem mudanças de novo, depois do C |

Dataset: **4.000 alunos, 90 turmas, 4.000 matrículas ativas** (uma por aluno; o modelo permite
só uma matrícula ativa por aluno). Nomes de pt_BR gerados com Faker; há nomes repetidos, o que
exercita a colisão de e-mails.

## Ambiente

Windows 11 Pro, Docker Desktop 29.8 (WSL2, 12 CPUs e ~7,7 GiB visíveis ao Docker). Containers
`python:3.12-slim` (httpx 0.28, uvicorn 0.54 com uvloop/httptools, SQLAlchemy 2.1, asyncpg) e
`postgres:16`. Tudo roda na **mesma máquina**, que estava em uso normal durante as medições.
O "provedor" é um mock em memória, não um provedor real.

## Resultado principal (configuração padrão)

Padrões: `PROVIDER_CONCURRENCY=10`, sem limite de taxa, sem falhas injetadas, latência do mock
de ~1 ms. Três execuções completas, mesma semente:

| Cenário | Execução 1 | Execução 2 | Execução 3 | Ações | Requests ao provedor |
|---|---:|---:|---:|---:|---:|
| A0 dry-run | 1,61 s | 1,43 s | 1,50 s | 8.090 (só plano) | 2 |
| **A abertura** | **22,27 s** | **20,60 s** | **20,65 s** | 8.090 | 8.092 |
| **B sem mudanças** | **0,52 s** | **0,44 s** | **0,53 s** | **0** | 99 |
| **C alterações** | **2,43 s** | **2,42 s** | **2,30 s** | 805 | 904 |
| B2 sem mudanças | 0,57 s | 0,61 s | 0,47 s | 0 | 105 |

- **A:** 4.000 `CREATE_USER` + 4.000 `ADD_TO_CLASS` + 90 `CREATE_CLASS`, 0 retries, 0 falhas.
  ≈ 360-390 ações/s. Por fase (execução 1): busca dos estados 265 ms, reconciliação 55 ms,
  execução 21.036 ms.
- **B:** 0 ações. O custo é só ler: 17 requests ao acadêmico e 99 ao provedor (8 páginas de
  usuários, 1 de turmas e os membros de cada uma das 90 turmas), mais a reconciliação (7 ms).
- **C:** 805 ações (100 `CREATE_USER`, 300 `ADD_TO_CLASS`, 300 `REMOVE_FROM_CLASS`,
  100 `SUSPEND_USER`, 5 `CREATE_CLASS`).
- Em todos os cenários o provedor terminou idêntico ao acadêmico, e os requests contados pelo
  cliente (telemetria) e pelo servidor (mock) coincidem (8.092 em A).
- A variação entre execuções foi de ±1 s em A. Três execuções não bastam para estatística fina.

## O resultado é bom? Onde está o gargalo

**A abertura leva ~20 s para 8.090 operações; o dia sem mudanças, ~0,5 s.** Para o objetivo
do projeto (uma sincronização diária) isso é adequado, e o caso comum (B) é muito barato. Mas
a abertura **poderia ser ~3× mais rápida**, e o motivo é uma descoberta incômoda:

1. **O gargalo é a biblioteca HTTP do cliente (httpx), não o banco nem o algoritmo.**
   `scripts/http_client_probe.py` (dentro da rede do compose, mesmo servidor, 3.000 `POST /users`,
   uma execução por ponto):

   | Simultâneas | httpx | aiohttp |
   |---:|---:|---:|
   | 3 | 925 req/s | 2.581 req/s |
   | 5 | 786 req/s | 2.793 req/s |
   | 10 | 500 req/s | 2.924 req/s |
   | 25 | 409 req/s | 2.602 req/s |

   O httpx tem teto de ~900 req/s em um processo e **piora com mais simultaneidade**; o aiohttp
   no mesmo servidor fica em ~2.600-2.900 req/s. Trocar de biblioteca contrariaria a stack
   combinada (httpx + respx para testes), então **não troquei**. Fica como a otimização de maior
   retorno.
2. **Mais concorrência nem sempre ajuda.** Varredura de `PROVIDER_CONCURRENCY` contra o mock
   local (latência ~1 ms), cenário A:

   | Simultâneas | A | C | ações/s em A |
   |---:|---:|---:|---:|
   | 1 | 18,40 s | 2,37 s | 440 |
   | 3 | 14,06 s | 1,88 s | 575 |
   | 5 | 14,55 s | 2,27 s | 556 |
   | 10 (padrão) | 22,27 s | 2,43 s | 363 |
   | 25 | 23,66 s | 2,20 s | 342 |

   Com um provedor **mais lento** a conclusão se inverte. Com 20 ms de latência por chamada:

   | Simultâneas | A | B | C | ações/s em A |
   |---:|---:|---:|---:|---:|
   | 3 | 70,18 s | 0,91 s | 7,80 s | 115 |
   | 10 | 24,23 s | 0,81 s | 3,18 s | 334 |
   | 25 | 32,26 s | 0,78 s | 3,98 s | 251 |

   Regra prática: a vazão sobe com a simultaneidade enquanto o *provedor* é o limite (latência
   alta) e cai quando o *cliente* satura (provedor rápido). Mantive o padrão em **10**, que é o
   melhor dos valores testados com latência de 20 ms; contra o mock local ele leva ~58% mais
   tempo que o ótimo (3-5 simultâneas: 14,1-14,6 s contra 22,3 s). Em produção, ajuste `PROVIDER_CONCURRENCY` medindo.
3. **O mock também era gargalo, e foi corrigido para o benchmark ser justo.** Na primeira
   medição o `mock-provedor` (middleware `BaseHTTPMiddleware`) saturava em ~700 req/s com 1, 2 ou
   3 geradores de carga. Troquei por middleware ASGI puro e `uvicorn[standard]`: 3 geradores
   somam ~1.670 req/s. (Os números acima já usam o mock corrigido.)
4. **Persistência no banco: 3× mais barata, sem efeito visível no total.** Com um provedor
   instantâneo (sem rede; banco acessado pela porta publicada no host), cada lote de 200 resultados custava 65,6 ms (UPDATE em `executemany`,
   um comando por linha). Troquei por um único `UPDATE ... FROM unnest(...)` por lote: 21,0 ms.
   Mas no benchmark completo o ganho some no ruído (A: 18,9-20,6 s antes, 20,6-22,3 s depois;
   variação normal entre execuções), porque o HTTP domina. Mantive a mudança (menos comandos,
   mais previsível), sem vender ganho que não medi.

### Limite de taxa e respostas 429

Provedor limitado a 300 req/s (`Retry-After: 1`), cenário A:

| Cliente | A | Requests | 429 | Retries | Falhas | Resultado |
|---|---:|---:|---:|---:|---:|---|
| sem limitador (`PROVIDER_MAX_RPS=0`) | 46,01 s | 8.352 | 260 | 260 | 0 | consistente |
| limitador de 250 req/s | 34,81 s | 8.092 | **0** | 0 | 0 | consistente |

Sem limitador, o cliente passa do limite e respeita cada 429 esperando o `Retry-After` (e depois
repete); chega ao resultado correto, mas **~32% mais devagar** e gastando 260 requests rejeitados.
O limitador evita os 429 e **não ataca o provedor**. O ritmo medido (232 req/s) fica abaixo
dos 250 configurados.

### Falhas injetadas

5% de respostas 5xx antes de processar e 2% de respostas perdidas (a operação é aplicada, a
resposta some), mesma semente:

| Cenário | Duração | Ações | 5xx | Retries | Falhas | Provedor == acadêmico |
|---|---:|---:|---:|---:|---:|:---:|
| A abertura | 43,12 s | 8.090 | 571 | 571 | **0** | sim |
| B | 0,92 s | **0** | 2 | 2 | 0 | sim |
| C | 8,02 s | 805 | 77 | 77 | 0 | sim |
| B2 | 1,52 s | **0** | 12 | 12 | 0 | sim |

Todas as respostas perdidas foram reconhecidas (409 por `external_id`) sem duplicar usuários, e
a sync seguinte teve 0 ações. O custo das falhas aqui é o backoff (0,5 s base, jitter total).

## Banco: N+1, índices e consultas

**N+1: não existe.** Não há relacionamentos ORM (nenhum carregamento preguiçoso possível) e as
consultas são em lote. Verificação automática em
`tests/integration/test_scale_and_telemetry.py`: contando comandos SQL, 20 alunos geram 15 comandos
e 400 alunos (805 ações), 36; a sync sem ações gasta 9, **independente do tamanho**. Os comandos
crescem só com o número de lotes (50 ações cada no teste; 200 por padrão), nunca por aluno.
(Detalhe: um `executemany` conta como 1 comando; por isso o UPDATE passou a ser um só.)

Planos de execução (`EXPLAIN ANALYZE`) com **410.670 linhas em `sync_actions`** (as de todos os
benchmarks) e 4.195 mapeamentos, para um run de 8.090 ações:

| Consulta | Plano | Tempo |
|---|---|---:|
| ações pendentes/bem-sucedidas do run, por `seq` | índice `(run_id, status)` + sort | 10,4 ms |
| status de todas as ações do run | índice `(run_id, status)` | 3,8 ms |
| contagem por status (fechar o run) | *index-only scan* | 3,0 ms |
| página de ações (`OFFSET 4000 LIMIT 100`) | `UNIQUE (run_id, seq)` | 3,0 ms |
| últimos runs | sort sobre 121 linhas (seq scan, tabela minúscula) | 0,3 ms |
| mapeamentos (lidos inteiros, por desenho) | seq scan de 4.195 linhas | 1,1 ms |
| UPDATE em lote de 200 ações por chave primária | `pk_sync_actions` | 6,2 ms |

Os índices atuais bastam; nenhuma consulta é gargalo. Ponto de atenção: paginação por `OFFSET`
lê todas as linhas anteriores (7.194 buffers para a posição 4.000); com runs muito maiores,
paginação por chave (`seq > X`) seria melhor.

## Concorrência (o que roda em paralelo, sempre limitado)

| Etapa | Paralelismo | Limite (variável) |
|---|---|---|
| Buscar estado desejado e atual | em paralelo entre si | — |
| Páginas de uma listagem e membros por turma (90 chamadas) | sim | `SNAPSHOT_CONCURRENCY` (5) |
| Ações dentro de uma fase | sim | `PROVIDER_CONCURRENCY` (10) |
| Ritmo de requisições ao provedor | espaçadas | `PROVIDER_MAX_RPS` (0 = desligado) |
| Fases entre si, reconciliação e gravação no banco | **não** (barreira / uma rotina) | — |

Nada é ilimitado: tudo passa por `asyncio.Semaphore` ou pelo limitador de taxa.

## O que poderia ser otimizado (não feito, em ordem de retorno esperado)

1. **Cliente HTTP mais rápido** (aiohttp, ou httpx com outro transporte): a sonda sugere ~3×
   na abertura, mas exige reescrever os testes que usam respx.
2. **Várias réplicas/processos do cliente** repartindo as ações (o httpx escala por processo).
   Exigiria um coordenador, o que foge da decisão de não ter fila/Celery neste projeto.
3. **APIs em lote do provedor** (provedores reais costumam ter): menos requests por ação.
4. **Persistir o lote N enquanto o lote N+1 executa** (hoje há uma barreira por lote).
5. Paginação por chave nas consultas de ações; `allocate_email` é O(n²) no pior caso de
   milhares de homônimos exatos (em um profile, 1.500 "João Silva" idênticos custaram ~0,55 s).

## Limitações deste benchmark

- Uma máquina, Docker Desktop/WSL2, computador em uso; 3 execuções no cenário padrão e **1**
  nas demais variações.
- O provedor é um mock em memória com latência de ~1 ms (ou 20 ms quando indicado). Um
  provedor real terá outra latência, cotas e comportamento de rede.
- A abertura mede criar 4.000 usuários; não mede, por exemplo, e-mails com colisão em massa.
- Os tempos incluem o *polling* de 100 ms do script para detectar o fim do run.
