# edu-sync — perguntas de entrevista

Perguntas e respostas técnicas sobre **o que está implementado** neste repositório. Cada resposta
aponta o arquivo ou o teste onde a afirmação pode ser conferida. Números marcados como
*medido* vêm de [docs/benchmark.md](docs/benchmark.md).

## Antes de tudo: o que NÃO existe

Para não prometer demais numa conversa:

- **Não há provedor real.** Só o `mock-provedor`. Não existe integração com Google.
- **Não há agendador** (a execução diária automática) **nem logs em JSON.** A sincronização é
  disparada por `POST /sync-runs`; os logs usam o `logging` padrão do Python.
- **Não há fila, workers, eventos/webhooks, métricas externas nem tracing.** O que existe são
  métricas por run gravadas no banco (`sync_runs.metrics`: tempos, requests, retries).
- **Não há autenticação** nas APIs.
- **Roda em uma instância.** O run é uma tarefa `asyncio` do próprio processo.

---

## 1. Como funciona a reconciliação?

`reconcile(desired, current)` em `domain/reconcile.py` recebe dois estados e devolve a lista de
ações. É uma função pura: não faz HTTP, não acessa banco, não lê relógio, não sorteia nada e não
altera as entradas.

- **Estado desejado** (vem do acadêmico): alunos ativos, turmas e matrículas ativas.
- **Estado atual** (vem do provedor, já traduzido para ids de origem): usuários, turmas, membros
  e a lista de e-mails reservados. Só entram entidades com `external_id` (as que o sync possui);
  usuários e turmas "órfãos" nunca geram ação, mas seus e-mails ficam reservados.

Os passos: turmas que faltam → `CREATE_CLASS`; alunos que faltam → `CREATE_USER` (com e-mail
alocado); suspensos que voltaram → `REACTIVATE_USER`; membros que não deveriam existir →
`REMOVE_FROM_CLASS`; matrículas que faltam → `ADD_TO_CLASS`; usuários ativos que saíram →
`SUSPEND_USER`. Uma troca de turma é só a combinação "remove A, adiciona B".

A saída é ordenada por **fase** (`PHASE_BY_TYPE`) e, dentro da fase, pela chave da ação. A ordem
importa: remover antes de adicionar (o aluno nunca fica em duas turmas), reativar antes de
matricular (o provedor recusa matricular suspenso) e remover antes de suspender. Cada
`ADD_TO_CLASS` leva `depends_on` apontando para o `CREATE_USER`/`CREATE_CLASS` do mesmo plano.

O e-mail é determinístico (`domain/emails.py`): `primeironome.ultimosobrenome@example.edu`, sem
acento. Em colisão, sufixo numérico (`joao.silva2`, `joao.silva3`), alocando os alunos novos em
ordem de id contra os e-mails já reservados. Entrada inconsistente (matrícula para aluno
inexistente, dois vínculos ativos para o mesmo aluno) levanta `InvalidDesiredStateError`: o run
falha em vez de adivinhar.

**Testes:** `tests/unit/domain/test_reconcile.py` (casos de negócio em tabela) e
`test_reconcile_invariants.py` (invariantes em 300 cenários aleatórios com semente fixa).

## 2. Como você garante idempotência?

Em quatro camadas (descritas também no README):

1. **Algoritmo.** Compara estados. Se o atual já é o desejado, devolve `[]`. A suíte aplica o
   plano num provedor simulado (`sync_testkit/simulator.py`, que recusa ação fora de ordem ou
   redundante) e verifica que reconciliar de novo devolve zero ações.
2. **Serviço.** O plano é sempre recalculado do estado observado, não lido de execuções
   anteriores. Duas execuções simultâneas são impedidas por um **índice único parcial** no
   PostgreSQL (`ux_sync_runs_single_running`, só para runs reais em `running`).
3. **Chamadas ao provedor.** São seguras de repetir, e o adapter traduz o resultado
   (`providers/mock_adapter.py`): `409` por `external_id` → `already_applied` e adota o id
   existente; `409` de membro duplicado → `already_applied`; `404 MEMBERSHIP_NOT_FOUND` ao
   remover → `already_applied`; suspender/reativar repetido responde 200.
4. **Persistência.** `UNIQUE (run_id, action_key)`, `UNIQUE` nos dois sentidos em `id_mappings`
   e upsert dos mapeamentos.

O caso difícil, **"o provedor aplicou, mas a resposta se perdeu"**, é coberto pelo item 3: o
reenvio recebe `409` e a ação conclui sem duplicar (teste
`test_resposta_perdida_e_reenvio_nao_duplicam_nada`: `POST /users` enviado 2 vezes, 1 usuário
criado, `attempts=2`, `outcome=already_applied`). Se o processo cai depois de o provedor aplicar
e antes de gravar o resultado, a próxima sincronização vê o usuário no snapshot e não gera ação.

**Prova de ponta a ponta:** `test_primeira_sync_aplica_as_acoes_e_a_segunda_gera_zero` (8 ações na
primeira, 0 na segunda e **nenhuma chamada de escrita** ao provedor na segunda).

## 3. O que acontece se o provedor cair?

Depende de quando:

- **Antes de começar:** a leitura do snapshot esgota o retry e o run termina `failed`, com a
  mensagem em `error`, 0 ações e nada executado
  (`test_provedor_fora_do_ar_antes_de_comecar_falha_o_run_sem_executar_nada`).
- **No meio da execução:** cada ação tenta até 5 vezes. Quando `MAX_CONSECUTIVE_FAILURES` (25)
  ações seguidas esgotam as tentativas (`RETRIES_EXHAUSTED`), o executor **para**
  (`ActionExecutor.run`), marca o resto como `skipped` com `RUN_ABORTED` e o run termina
  `aborted`. Sem isso, 4.000 ações × 5 tentativas × backoff manteriam o run preso por muito
  tempo contra um provedor morto. Quando o provedor volta, `POST /sync-runs/{id}/retry-failed`
  retoma só o que não terminou (`test_provedor_cai_no_meio_aborta_e_retry_failed_conclui`).
- **Falhas permanentes** (por exemplo, `400`) não contam para esse limite: indicam problema nos
  dados, não no provedor (`test_falhas_permanentes_seguidas_nao_acionam_o_circuit_breaker`).

O que **não** está resolvido: se o banco do próprio sync cair na hora de fechar o run, o run fica
`running` até um restart (que o marca `interrupted`) e bloqueia novos runs reais nesse meio tempo.

## 4. Como funciona o retry?

`resilience/retry.py` (`retry_async`) repete uma operação enquanto o erro for transitório:

- **Quais erros:** decididos por `resilience/errors.py` (`classify_status`,
  `classify_transport_error`). Transitórios: `408, 429, 500, 502, 503, 504` e falhas de rede
  (timeouts, conexão, erro de protocolo). O resto é permanente.
- **Quanto:** 5 tentativas no total (a primeira conta), atraso base 0,5 s, fator 2, teto 30 s,
  **jitter total** (o atraso real sorteia entre 0 e o teto: 0,5 → 1 → 2 → 4 s), mais um orçamento
  de 120 s por operação. Tudo por variável de ambiente (`RETRY_*`).
- **`Retry-After`:** se o servidor pediu, vira o piso do atraso, limitado a 60 s.
- **Onde:** em volta de **cada ação** (`ActionExecutor.execute_action`, que também conta as
  `attempts` gravadas no banco) e de **cada página** lida (`clients/paged.py`). As duas unidades
  são idempotentes, então repetir é seguro. O adapter de escrita **não** repete sozinho: quem
  repete é o executor, para o contador de tentativas ser fiel.
- **Testabilidade:** `sleep`, `uniform` e o relógio são injetados. Os testes verificam a sequência
  exata de atrasos sem esperar (`tests/unit/resilience/test_retry.py`).

## 5. Por que 429 merece retry?

Porque é um sinal de "calma, tente já já", não de "seu pedido está errado". O pedido é válido e
vai funcionar quando a cota renovar. O servidor ainda diz quanto esperar (`Retry-After`), e o
cliente respeita (`test_429_com_retry_after_espera_pelo_menos_o_que_o_servidor_pediu`).

Medido: com o provedor limitado a 300 req/s e **sem** limitador no cliente, a abertura teve 260
respostas 429, todas repetidas, 0 falhas e terminou correta em 46,0 s. Com o limitador do cliente
(`PROVIDER_MAX_RPS=250`) foram 0 respostas 429 e 34,8 s. O 429 é recuperável, mas é melhor não
provocá-lo: por isso o cliente também sabe se limitar (`resilience/rate_limit.py`).

## 6. Por que 400 não merece retry?

Porque o resultado é o mesmo toda vez. Um `400`/`422` diz que o **pedido** é inválido; repetir
só gasta cota, atrasa o run e esconde um bug nos dados. O mesmo vale para `401`, `403` e `404`
inesperado. Então a ação falha **na primeira tentativa** (`attempts=1`), com o `error_code` do
provedor (por exemplo `VALIDATION_ERROR`, `USER_SUSPENDED`), e fica visível para investigação
(`test_400_nao_faz_retry`: 1 chamada HTTP, nenhuma espera).

Dois cuidados: alguns `409`/`404` **não** são erros, e sim "o estado desejado já valia" (viram
`already_applied`); e um `409` por **e-mail** de outra pessoa é permanente (`EMAIL_CONFLICT`),
porque o e-mail alocado ficou obsoleto.

## 7. Como você trata falhas parciais?

Uma ação que falha não interrompe as demais. `ActionExecutor.execute_action` **nunca levanta**:
devolve um resultado (`succeeded` ou `failed`) com tentativas, último erro e horário. Se o erro é
inesperado (um bug), vira `UNEXPECTED_ERROR` e é logado, sem derrubar o run.

Ações cujas dependências falharam são **puladas** (`skipped`, `DEPENDENCY_FAILED`) sem chamar o
provedor: se criar o usuário falhou, matriculá-lo só geraria um erro em cascata confuso. No fim, o
run vira `partial`, e os contadores (`successful/failed/skipped_actions`) vêm de **agregação no
banco**, não de contagem em memória. Teste: `test_falha_parcial_nao_aborta_e_fica_registrada`
(8 ações: 6 ok, 1 falha, 1 pulada, e o resto foi aplicado).

## 8. Como funciona o retry de ações falhadas?

`POST /sync-runs/{id}/retry-failed` (`store.begin_retry` + `SyncRunner.execute_retry`):

- **Elegíveis:** `failed`, `pending` (run interrompido) e `skipped` por `DEPENDENCY_FAILED` ou
  `RUN_ABORTED`. As `succeeded` nunca são tocadas.
- **Sem recalcular o plano.** Os ids do provedor vêm de `id_mappings`, então nem busca o estado de
  novo (um teste confirma que não há leituras). Fica seguro porque cada operação é idempotente.
- **Sem corrida:** a transação trava o run (`FOR UPDATE`) e o move para `running`; se outro run
  real está ativo, o índice único recusa e a API responde `409 RUN_IN_PROGRESS`. Em run de
  dry-run responde `409 RUN_IS_DRY_RUN`; sem nada elegível responde `200` sem fazer nada.
- **Auditoria:** `attempts` acumula (1 da execução original + 1 do retry), e o run guarda
  `retry_count` e `last_retry_at`.

Limitação real: executa o plano antigo. Depois de um retry, vale rodar uma sincronização nova.

## 9. Como evitar N+1?

- **Não há relacionamentos ORM**, então não existe carregamento preguiçoso que dispare uma consulta
  por linha.
- **Escritas em lote:** o plano é inserido em blocos de 1.000 (`store.insert_actions`); os
  resultados de cada lote viram **um** `UPDATE ... FROM unnest(...)` (`store.persist_results`);
  os mapeamentos são lidos em uma consulta e sincronizados com um `DELETE` e um `INSERT`.
- **Verificação, não só intenção:** `test_numero_de_consultas_nao_cresce_com_o_numero_de_alunos`
  conta os comandos SQL com um listener do SQLAlchemy. Medi 15 comandos para 20 alunos e 36 para
  400 alunos (805 ações); a sincronização sem ações gasta 9, qualquer que seja o tamanho. Os
  comandos crescem com o número de **lotes**, nunca de alunos.
- **Planos e índices:** com 410 mil linhas em `sync_actions`, as consultas principais usam
  índice e levam de 0,3 a 10 ms (`EXPLAIN ANALYZE`, em [docs/benchmark.md](docs/benchmark.md)).

Uma ressalva honesta: o `UPDATE` por linha em `executemany` era a versão anterior; trocá-lo por um
único comando reduziu o custo isolado de 65 para 21 ms por lote, mas **não** apareceu no benchmark
completo, porque o HTTP domina.

## 10. Como escalar para 4.000 alunos?

Já foi medido, com o dataset gerado por Faker (4.000 alunos, 90 turmas, 4.000 matrículas):

| Cenário | Duração (3 execuções) |
|---|---|
| Abertura do período (8.090 ações) | 20,6 a 22,3 s |
| Dia sem mudanças (0 ações) | 0,44 a 0,53 s |
| 100 novos, 100 saíram, 200 trocas, 5 turmas (805 ações) | 2,30 a 2,43 s |

O dia comum custa só leituras (17 requests ao acadêmico, 99 ao provedor). O que sustenta a
escala: leitura paginada com concorrência limitada, execução em lotes com semáforo, escrita no
banco em lote e retry por ação.

O **gargalo medido** é a biblioteca HTTP do cliente: numa sonda (`scripts/http_client_probe.py`),
o `httpx` chegou a ~930 req/s e piorou com mais simultaneidade, enquanto o `aiohttp` fez ~2.600 a
2.900 req/s no mesmo servidor. O banco e o algoritmo não são o problema. Para ir além: outro
cliente HTTP, APIs em lote do provedor, ou dividir ações entre processos (o que pediria
coordenação, ou seja, uma fila).

## 11. Como controlar concorrência?

Tudo com limite explícito, configurável:

| O quê | Como | Variável (padrão) |
|---|---|---|
| Ler acadêmico e provedor | `asyncio.gather` entre os dois | — |
| Páginas e membros por turma | 1ª página sequencial, o resto com `Semaphore` (`clients/pagination.py`, `MockProvedorAdapter.snapshot`) | `SNAPSHOT_CONCURRENCY` (5) |
| Ações dentro de uma fase | `Semaphore` no executor | `PROVIDER_CONCURRENCY` (10) |
| Ritmo de requisições | espaçamento por `AsyncRateLimiter` (retries inclusive) | `PROVIDER_MAX_RPS` (0 = desligado) |
| Dois runs ao mesmo tempo | índice único parcial no banco | — |

O que **não** é concorrente, de propósito: as **fases** (barreira, porque uma depende da anterior);
a **reconciliação** (síncrona, determinística); a **gravação no banco** (uma rotina só, porque
`AsyncSession` não é segura para uso concorrente; os workers só fazem HTTP e devolvem resultados).

Achado medido: mais concorrência nem sempre ajuda. Contra o mock local, 3 simultâneas foram mais
rápidas (14,1 s) que 10 (22,3 s) e 25 (23,7 s), porque o cliente satura. Com 20 ms de latência a
ordem se inverte (3: 70 s, 10: 24 s, 25: 32 s). Mantive 10 e documentei que se ajusta medindo.

## 12. Como você testou uma API externa?

Em camadas, sem depender da API real:

1. **`respx`** para o adapter e os clientes: respostas roteirizadas (`429→429→200`, `500→200`, `400`,
   timeouts) com contagem de chamadas.
2. **Os mocks são aplicações FastAPI de verdade** (`mock-academico`, `mock-provedor`) com falhas
   injetáveis: 429 com `Retry-After`, 5xx, latência, limite de taxa e "aplicou mas a resposta
   se perdeu". Nos testes de integração rodam **em processo**, ligados ao `sync-service` por um
   transporte HTTP (`ASGITransport`), então o adapter real conversa com um servidor real.
3. **`FaultyTransport`** (`tests/sync_testkit/transports.py`) injeta falhas entre o sync e os mocks:
   resposta perdida, provedor fora do ar, `400` para um aluno específico.
4. **Testes dos próprios mocks**, para o contrato que o adapter assume.
5. **CI com `docker compose`** subindo a stack e **benchmark** com falhas injetadas.

Limite honesto: o mock é a **minha interpretação** de como um provedor se comporta. Não há teste
de contrato contra um provedor real.

## 13. Por que utilizar respx?

Porque intercepta o `httpx` na camada de transporte: sem servidor, sem rede, sem portas, rápido e
determinístico. Permite roteirizar sequências (`side_effect=[429, 429, 200]`), simular timeouts e
afirmar quantas chamadas ocorreram (`route.call_count`), que é exatamente a prova de "houve
retry" ou "não houve retry". Como o cliente do projeto é `httpx`, o `respx` simula os problemas
sem mudar o código de produção.

Limites: acopla o teste ao `httpx` (trocar de biblioteca exigiria reescrevê-los) e não prova que o
provedor real se comporta como o mock. Por isso os testes de integração também usam servidores
simulados de verdade.

## 14. Por que PostgreSQL real no CI?

Porque o projeto usa recursos que SQLite não tem ou trata diferente: **índice único parcial** (a
trava de "um run por vez"), `CHECK`, `FK` com cascata, `ON CONFLICT`, JSONB e `unnest` com arrays.
Um teste que passa em SQLite poderia falhar (ou, pior, passar) sem provar nada sobre produção.

No CI, o job `test` sobe um `postgres:16` como *service container* e roda `pytest -m integration`.
Esses testes verificam, no banco real: mapeamento duplicado rejeitado, segundo run real em
`running` rejeitado (e 8 `create_run` simultâneos: só um vence), dry-runs sem competir pelo lock,
status inválido rejeitado, `DELETE_USER` impossível, e que a **migration bate com os modelos**
(`compare_metadata`).

## 15. Por que não usar Redis?

Porque não há problema que ele resolva aqui. A exclusão mútua (o lock de execução) está no próprio
PostgreSQL, que já guarda o estado e a auditoria, e fica visível como dado e sobrevive a restart. A
concorrência é de I/O dentro de um processo (`asyncio`). Cache não é necessário: a leitura do dia
sem mudanças custa ~0,5 s. Adicionar Redis traria outra peça para operar e outra fonte de verdade
sem ganho mensurável.

Onde faltaria: com **várias instâncias** o lock por índice continua valendo, mas o resto (marcar
runs interrompidos no startup, a tarefa em memória) assume uma instância. Aí caberia uma fila ou
um coordenador, e esta é uma limitação reconhecida, não resolvida.

## 16. Como adicionaria um provedor real?

Escrevendo um novo adapter que implemente `ProvedorDeContas` (`providers/base.py`), sem alterar
`reconcile`, o executor nem a API. São sete métodos: `snapshot`, `create_class`, `create_user`,
`suspend_user`, `reactivate_user`, `add_member` e `remove_member`. O trabalho está em:

- **Traduzir respostas** para o contrato: `Outcome.APPLIED` / `ALREADY_APPLIED` e as exceções
  `TransientUpstreamError` / `PermanentUpstreamError`. Reaproveita
  `resilience/errors.py` (`error_from_response`, `error_from_transport`) e os padrões de retry.
- **Descobrir o que cada código de erro significa naquele provedor**: o "já existe" e o "já foi
  removido" podem ter outro formato, e isso é responsabilidade do adapter.
- **Autenticação, cotas e paginação** do provedor; `PagedReader` cobre paginação por página/offset,
  então token de próxima página exigiria adaptação.
- **Testes** com `respx` para o contrato HTTP, como os do `MockProvedorAdapter`.

**Ponto fraco real:** hoje o snapshot é traduzido por `sync/snapshot.py`, que depende de o
provedor devolver o `external_id` dos recursos. Um provedor que não guarde esse identificador
exigiria que o adapter montasse o vínculo a partir de `id_mappings`. Isso **não está
implementado**.

## 17. Como transformaria isso em arquitetura orientada a eventos?

Hoje **não é** orientado a eventos; é reconciliação periódica disparada por API. Seria um
caminho evolutivo, não uma reescrita:

- Manter a reconciliação completa como **rede de segurança**: ela converge mesmo se um evento se
  perder.
- Receber eventos do acadêmico (webhooks ou mudanças capturadas) e colocá-los numa fila,
  disparando uma reconciliação **limitada aos alunos afetados**. Atenção: `reconcile` assume
  estados completos (por exemplo, "quem não está no desejado é suspenso"), então um recorte
  precisaria ser aplicado de forma consistente nos dois lados.
- Como as ações já são idempotentes, a entrega **pelo menos uma vez** da fila seria suportada sem
  mudanças.
- Isso traz o que o projeto evitou de propósito (broker, workers, ordenação, deduplicação), e só
  se justifica se a latência de uma execução diária for um problema real.

## 18. O que você faria diferente em produção?

Em ordem do que eu atacaria primeiro:

1. **Autenticação e segredos** nas APIs; hoje são abertas, com portas presas a `127.0.0.1`.
2. **Agendador e logs estruturados em JSON** (previstos no escopo inicial, não implementados), além
   de métricas e tracing.
3. **Run preso:** detecção por heartbeat (hoje, se o banco cai ao fechar o run, só um restart
   libera) e marcar `interrupted` por run/instância, não "todos os `running`".
4. **Mais de uma instância** com o coordenador adequado (fila ou lease), se for preciso.
5. **Provedor real** com seus erros, cotas e lotes, e testes de contrato contra um ambiente de
   sandbox dele.
6. **Retenção de histórico** (runs e ações crescem indefinidamente: 410 mil linhas só com os
   benchmarks), paginação por chave nas ações e um limite no tamanho da resposta do dry-run.
7. **Versões travadas** (lockfile) e imagens com versões fixas; hoje as dependências são faixas.
8. **Desempenho:** trocar o cliente HTTP ou usar APIs em lote, porque é o teto medido.
9. **Alertas** quando um run termina `partial` ou `aborted`, que hoje só aparece se alguém consultar.

---

## Números para citar (todos medidos, veja [docs/benchmark.md](docs/benchmark.md))

| O quê | Valor |
|---|---|
| Dataset | 4.000 alunos · 90 turmas · 4.000 matrículas ativas |
| Abertura do período | 20,6 a 22,3 s (8.090 ações) |
| Dia sem mudanças | 0,44 a 0,53 s (**0** ações) |
| Dia com mudanças (805 ações) | 2,30 a 2,43 s |
| Com 5% de 5xx + 2% de respostas perdidas | 43,1 s · 571 retries · **0 falhas** |
| Provedor com limite de 300 req/s | 46,0 s sem limitador (260 × 429) · 34,8 s com (0 × 429) |
| Teto do cliente HTTP | `httpx` ~930 req/s · `aiohttp` ~2.600-2.900 req/s |
| Comandos SQL | 15 (20 alunos) · 36 (400 alunos) · 9 (sync sem ações) |
| Testes | 3.562 sem banco · 50 de integração com PostgreSQL real |
