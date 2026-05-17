# orch

Aplicação de workflow/orquestração orientada a eventos externos (webhooks e payloads diversos), com foco inicial em detecção de origem do payload e controle de sessões no PostgreSQL.

## Fase 1 — Objetivo

Nesta fase, o foco é:

1. Preparar a base de dados da tabela `orch_sessions`.
2. Definir regras de identificação de origem do payload.
3. Definir regras de criação/reuso de sessão ativa.
4. Preparar a estrutura para próxima etapa de API FastAPI.

> Nesta entrega, a implementação da API ainda não foi iniciada. O artefato principal é o SQL da tabela.

## Stack

- Python 3.12+
- FastAPI (implementação prevista para próxima etapa)
- PostgreSQL
- RabbitMQ (fase 2)
- Redis (fase 2 em diante)

## Ambiente local

## 1) Ativar `.venv`

No diretório raiz do projeto:

```bash
source .venv/bin/activate
```

## 2) Instalar dependências

Com o `requirements.txt` da fase 1:

```bash
pip install -r requirements.txt
```

## 3) Configurar `.env`

Este projeto utiliza arquivo `.env` local para credenciais/configurações do PostgreSQL.

Exemplo de variáveis esperadas:

```env
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=orch
DATABASE_USER=postgres
DATABASE_PASSWORD=postgres
DATABASE_SCHEMA=ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60
DATABASE_ECHO=false
DATABASE_USE_NULL_POOL=true
DATABASE_POOL_SIZE=20
DATABASE_MAX_OVERFLOW=40
DATABASE_POOL_TIMEOUT=30
DATABASE_POOL_RECYCLE=1800
WORKFLOW_V2_ENABLED=false
WORKFLOW_V2_EXECUTE_M2=false
WORKFLOW_V2_MAX_STEPS=25
```

> Ajuste os valores conforme seu ambiente real.
> Na fase 1, o schema é único e fixo via `DATABASE_SCHEMA`. A evolução para multi-schema fica para fases futuras.
> Com PgBouncer, o padrão recomendado aqui é `DATABASE_USE_NULL_POOL=true` para evitar dupla camada de pool no app.
> Com PgBouncer, o `search_path` é aplicado por sessão na aplicação (não como startup parameter).

## Execução da aplicação (próxima etapa)

Quando a API FastAPI for implementada:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 7777 --reload
```

## Endpoint previsto

- `POST /v1/orch/{flow_uuid}`
- Status atual: implementado com detecção, extração e persistência transacional (`202 Accepted`).
- Endpoints de consulta operacional já implementados:
  - `GET /v1/orch/sessions/{session_uuid}`
  - `GET /v1/orch/sessions/by-flow/{flow_uuid}?limit=50&cursor=...`
  - `GET /v1/orch/sessions/by-entity?entity=...&entity_type=...&entity_address=...&limit=50&cursor=...`
  - `GET /v1/orch/alarms?level=warning|error&code=...&flow_uuid=...&session_uuid=...&app_name=...&limit=50&cursor=...`
  - `POST /v1/orch/{workspace_uuid}/{flow_uuid}/sessions` (criação explícita de sessão por app integradora)
    - `entity_session_id` é gerado internamente pelo ORCH no formato `entity_address:::flow_uuid`.
    - `assigned_at` é preenchido automaticamente com `NOW()` quando ausente.
  - `POST /v1/orch/{workspace_uuid}/{flow_uuid}/sessions/unassign`
    - recebe `entity_address` e marca `unassigned_at = NOW()` nas sessões correspondentes (quando `unassigned_at` ainda é `NULL`);
    - também marca `state=5 (stopped_after_unassign)` e garante `ended_at`.

## Endpoints de health (já implementados)

- `GET /health/live`
- `GET /health/db`
- `GET /health/ready` (valida conectividade, schema ativo e existência de `orch_sessions`)

## Segurança do `/docs` (produção)

- O acesso a `/docs`, `/redoc` e `/openapi.json` é bloqueado para origem externa por padrão.
- Faixa interna padrão permitida: `10.1.20.0/24` (além de loopback).
- A validação considera `X-Forwarded-For` apenas quando o cliente direto está em proxy confiável.

Variáveis de ambiente:

```env
DOCS_ACCESS_CONTROL_ENABLED=true
DOCS_INTERNAL_CIDRS=10.1.20.0/24,127.0.0.1/32,::1/128
DOCS_TRUSTED_PROXY_CIDRS=10.1.20.0/24,127.0.0.1/32,::1/128
```

## Endpoint `POST /v1/orch/{flow_uuid}` (base implementada)

- Aceita payload JSON genérico.
- Detecta App de origem nesta ordem: `ArquivosApp`, `WhatsApp`, `DialerApp`, `GenericApp`.
- Para `GenericApp`, exige ao menos `external_id`; sem isso retorna `422` com mensagem em pt-BR.
- Extrai campos mínimos de sessão por App: `entity`, `entity_type`, `entity_address`, `entity_session_id`.
- Persiste sessão na `orch_sessions` com regra de sessão ativa (`state <> 3 AND unassigned_at IS NULL`):
  - se já existir sessão ativa para a combinação (`flow_uuid`, `entity`, `entity_type`, `entity_address`), atualiza;
  - se não existir, cria nova sessão com `started_at=NOW()` e `state` derivado do evento.
- Para eventos WhatsApp, converte `statuses[].timestamp` (Unix string) e atualiza:
  - `sent` -> `whatsapp_sent_at`
  - `delivered` -> `whatsapp_delivered_at`
  - `read` -> `whatsapp_read_at`
  - `failed` -> `whatsapp_failed_at`
- Para eventos DialerApp, aplica mapeamento conservador de resultado e atualiza:
  - `success` -> `dialer_answered_at`
  - `busy` -> `dialer_busy_at`
  - `rejected` -> `dialer_rejected_at`
  - `invalidnumber` -> `dialer_invalid_number_at`
  - `noanswer` -> `dialer_not_answered_at`
  - `failure` -> `dialer_failed_at`
- Mapeamento de `state`/`ended_at` na fase 1:
- `DialerApp` com evento final de hangup -> `state=3 (finished)` e define `ended_at`
- `WhatsApp sent/delivered` -> `state=2 (waiting)`
- `WhatsApp read/failed` -> `state=3 (finished)` e define `ended_at`
- `ArquivosApp` e `GenericApp` -> `state=1 (running)`
- `unassign` manual por API -> `state=5 (stopped_after_unassign)` + `unassigned_at`
- Retorna `202 Accepted` com metadados de aceite, campos extraídos e metadados de persistência (`session_id`, `session_uuid`, `session_state`, `session_created`).
- Respostas de sucesso incluem `api_version = \"v1\"`.
- Quando `WORKFLOW_V2_ENABLED=true`, executa bootstrap do workflow (M1):
  - carrega revisão do fluxo (publicada maior versão; fallback draft);
  - injeta payload no runtime da sessão;
  - define `next_card_uuid` inicial;
  - retorna metadados em `workflow_bootstrap`.
- Quando `WORKFLOW_V2_EXECUTE_M2=true`, executa motor M2 após bootstrap:
  - executa `set_variables` e `condition`;
  - executa `scheduling_moment`/`wait` atualizando `frozen_until` e interrompendo o avanço;
  - executa `finish_flow` com `ended_at` e `state=3`;
  - atualiza `last_card_uuid` e `next_card_uuid` a cada transição;
  - para com segurança em componente fora do escopo e retorna `workflow_execution`.
- Para concorrência alta, usa lock transacional por chave lógica via `pg_advisory_xact_lock`.
- Em condição de corrida/out-of-order, tenta reaproveitar a sessão mais recente pela mesma chave de sessão (`flow_uuid + entity + entity_type + entity_address + entity_session_id`) antes de criar nova.
- Semântica de origem por sessão:
  - `entity_origin_app` representa a **origem inicial** da sessão (primeiro app que abriu/reusou a sessão) e não é usado como indicador do evento mais recente.
  - para identificar a app do evento **corrente**, usar `runtime_variables.source_app` e os snapshots `runtime_variables.last_payload` / `runtime_variables.last_extracted`.
- Organização atual do código:
  - detector central: `app/services/app_detector.py`
  - extração por App: `app/handlers/*.py`
  - orquestração de extração: `app/services/session_extractor.py`
  - persistência transacional: `app/services/session_service.py` + `app/repositories/orch_sessions_repository.py`

## Como subir localmente (base atual)

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 7777 --reload
```

## Comandos de migration (CLI)

Aplicar migrations do `orch` em todos os workspaces ativos:

```bash
source .venv/bin/activate
python -m app.cli migrate-all
```

Aplicar migrations do `orch` em um workspace específico:

```bash
source .venv/bin/activate
python -m app.cli migrate-workspace <workspace_uuid>
```

Guia obrigatório de processo (cirúrgico) para mudanças estruturais:
- `docs/MIGRATIONS_PLAYBOOK.md`

## Testes (concorrência de eventos)

### ATENCAO (OBRIGATORIO)

- TESTES COM DEPENDENCIA DE REDE/DB/API EXTERNA DEVEM RODAR FORA DA SANDBOX.
- SE A SANDBOX BLOQUEAR ACESSO, PEDIR ELEVACAO E REEXECUTAR.
- SEM EVIDENCIA EXTERNA (EX.: POST RECEBIDO NA API DE DESTINO), O TESTE NAO E CONSIDERADO CONCLUIDO.

```bash
source .venv/bin/activate
pytest -q tests/test_concurrency_parallel.py tests/test_out_of_order.py
```

## Testes de payloads (detecção e extração)

```bash
source .venv/bin/activate
pytest -q tests/test_payload_detection_and_extraction.py
```

Payloads de referência versionados:
- `tests/payloads/arquivos_app.json`
- `tests/payloads/whatsapp_sent.json`
- `tests/payloads/whatsapp_delivered.json`
- `tests/payloads/whatsapp_read.json`
- `tests/payloads/whatsapp_failed.json`
- `tests/payloads/dialer_app.json`
- `tests/payloads/generic_app.json`

## Testes de consulta operacional

```bash
source .venv/bin/activate
pytest -q tests/test_session_queries.py
```

As listagens por `flow` e `entity` usam paginação por cursor estável (`created_at + id`), retornando `next_cursor` quando houver próxima página.

## Testes de alarmes

```bash
source .venv/bin/activate
pytest -q tests/test_alarms.py
```

## Governança e operação

- Regras finais de estado da fase 1: `docs/PHASE1_STATE_RULES.md`
- Runbook operacional da fase 1: `docs/RUNBOOK_PHASE1.md`

## Apps reconhecidas na fase 1

- `ArquivosApp`
- `WhatsApp`
- `DialerApp`
- `GenericApp`

## Regras de identificação de payload

## ArquivosApp

Sinais de identificação:
- presença de `file`
- `file.id`
- `file.original_name`
- `file.folder_path`
- sinais de evento S3/MinIO (`EventName`, `Records[].eventSource`)

Mapeamento inicial:
- `entity = file.id`
- `entity_type = file`
- `entity_address = file.folder_path + "/" + file.original_name`
- `entity_session_id = file.id`

## WhatsApp

Sinais de identificação:
- `object = whatsapp_business_account`
- `entry[].changes[].value.messaging_product = whatsapp`
- presença de `statuses[]`
- presença de `contacts[].wa_id`

Mapeamento inicial:
- `entity = contacts[0].wa_id` (ou `statuses[0].recipient_id`)
- `entity_type = person`
- `entity_address = contacts[0].wa_id` (ou `statuses[0].recipient_id`)
- `entity_session_id = contacts[0].wa_id`

Status de evento suportados na fase 1:
- `sent`
- `delivered`
- `read`
- `failed`

## DialerApp

Sinais de identificação:
- presença de `hangup`
- presença de `makecall`
- presença de `uniqueid`
- `hangup.Event = Hangup`
- `makecall.Event = DialBegin`

Mapeamento inicial:
- `entity = identificador disponível (fallback: telefone)`
- `entity_type = person`
- `entity_address = telefone extraído`
- `entity_session_id = uniqueid | hangup.Uniqueid | hangup.Linkedid`

Extração de telefone:
1. `hangup.CdrMailingData`
2. fallback em `makecall.DialString`

## GenericApp

Identificação:
- não classificar como ArquivosApp/WhatsApp/DialerApp
- payload conter `external_id`

Mapeamento:
- `entity = external_id`
- `entity_type = api_request`
- `entity_address = external_id`
- `entity_session_id = external_id`

Se não for reconhecido e não houver `external_id`, retornar `422 Unprocessable Entity` (mensagem clara em pt-BR).

## Regra de abertura/reuso de sessão

A combinação lógica da sessão é:
- `flow_uuid`
- `entity`
- `entity_type`
- `entity_address`

Regra:
- se **não existir** registro ativo (`state <> 3` e `unassigned_at IS NULL`) para essa combinação, cria nova sessão;
- se **existir** registro ativo, atualiza sessão existente.

Estados de sessão:
- `0 = pending`
- `1 = running`
- `2 = waiting`
- `3 = finished`
- `5 = stopped_after_unassign`

## Estrutura da tabela `orch_sessions`

Script SQL desta entrega:
- `sql/001_create_orch_sessions.sql`
- `sql/002_add_entity_origin_app.sql`
- `sql/003_create_orch_sessions_alarms.sql`
- `sql/004_create_orch_session_metrics.sql`
- `sql/005_update_orch_session_metrics_for_async.sql`
- `sql/006_create_orch_generate_file_tables.sql`
- `sql/007_add_assigned_fields_to_orch_sessions.sql`
- `sql/008_fix_assigned_fields_to_timestamps.sql`
- `sql/009_add_orch_sessions_flow_entity_index.sql`
- `sql/010_create_orch_discarded_events.sql`
- `sql/011_allow_stopped_after_unassign_state.sql`

Campos incluídos conforme solicitado:
- `id`, `uuid`, `flow_uuid`, `state`
- `entity_origin_app`, `entity`, `entity_type`, `entity_address`, `entity_session_id`
- `started_at`, `ended_at`, `abandoned_at`, `frozen_until`
- `last_card_uuid`, `next_card_uuid`
- `runtime_variables`, `agent_interactions`
- `assigned_at`, `unassigned_at`
- status/timestamps de Dialer
- status/timestamps de WhatsApp
- campos reservados de SMS/RCS
- auditoria: `created_at`, `updated_at`

Além disso, o SQL inclui:
- `CREATE EXTENSION IF NOT EXISTS pgcrypto`
- `CHECK (state IN (0,1,2,3))`
- índices para busca por `flow_uuid`, chave de entidade, sessão ativa (`state <> 3`), `entity_session_id`, estado e timestamps de status.
- migração incremental para ambientes já existentes em `sql/002_add_entity_origin_app.sql`.
- tabela de alarmes operacionais em `sql/003_create_orch_sessions_alarms.sql`.
- tabela de auditoria de descartes em `sql/010_create_orch_discarded_events.sql`.

Observação importante:
- `entity_origin_app` é campo de origem histórica da sessão (origem inicial), enquanto a origem do evento mais recente deve ser lida de `runtime_variables.source_app`.

## Observabilidade

- Logging estruturado em JSON com `request_id`, path, status e latência.
- `X-Request-ID` aceito na entrada e retornado na resposta.
- Persistência best-effort de warnings/errors em `orch_sessions_alarms`.
- Persistência best-effort de eventos descartados em `orch_discarded_events`.
- Respostas de erro padronizadas com `api_version`, `code`, `detail` e `request_id`.
- Códigos de alarme atuais incluem:
  - `trigger_orch_http_exception`
  - `trigger_orch_unhandled_exception`
  - `query_flow_invalid_cursor`
  - `query_entity_invalid_cursor`

## Itens não implementados nesta fase

- SMS (somente campos na tabela)
- RCS (somente campos na tabela)
- RabbitMQ
- Redis

## Como vamos trabalhar

- Comunicação sempre em pt-BR.
- Sempre pedir permissão para ações com elevação de privilégio.
- Uso obrigatório da `.venv` no desenvolvimento local.
- Preservar payloads externos sem alterar contratos de terceiros.
- Adaptar lógica ao payload recebido, sem exigir mudanças dos sistemas de origem.

## Próximos passos da fase 1

1. Criar tabela manualmente no PostgreSQL usando `sql/001_create_orch_sessions.sql`.
2. Validar acesso ao banco via `.env`.
3. Implementar API FastAPI (`POST /v1/orch/{flow_uuid}`).
4. Implementar detector de App (ArquivosApp/WhatsApp/DialerApp/GenericApp).
5. Implementar handlers por App.
6. Implementar persistência de sessão com regra de sessão ativa (`state <> 3`).
7. Criar testes com payloads de referência.

## Planejamento — Fase 2 (checklist)

### Objetivo da fase 2

Executar workflows definidos em `flow_v2_revision.definition` a partir do `flow_uuid` recebido no `POST /v1/orch/{flow_uuid}`, com foco em performance, previsibilidade e base sólida para fase 3.

### Fontes de dados de fluxo (schema atual)

- `flow_v2`
- `flow_v2_revision`

Regra de seleção da revisão do fluxo:
- usar a maior `version` publicada;
- se não houver versão publicada, usar a revisão `draft`.

### Conceitos de execução

- Os componentes (cards) estão em `definition.components`.
- O encadeamento de execução está em `definition.branches`.
- Existem componentes bloqueantes e não bloqueantes.
- `scheduling_moment` deve registrar pausa de execução via `orch_sessions.frozen_until`.
- Durante execução, manter:
  - `last_card_uuid`
  - `next_card_uuid`
- `finish_flow` deve encerrar sessão:
  - definir `ended_at`
  - finalizar fluxo/sessão.

### Componentes da fase 2 (escopo)

- `condition`
- `set_variables`
- `api_call`
- `code_editor`
- `scheduling_moment`
- `finish_flow`

### Fluxos de teste já disponíveis

Payload de teste (GenericApp):

```json
{
  "external_id": "fffffff",
  "valor_recebido": 114
}
```

Flow UUIDs para validação:

1. `2cb9482a-131e-4b2a-8507-484745661836`
   - Injetar dados do payload recebido na sessão/runtime.
   - Encadear variáveis, comparação simples, `api_call` e finalização.

2. `fea492fb-9420-4690-ba09-bd73dca50717`
   - Usar mesma variável de entrada.
   - Avaliar número primo com componente que executa JS (`code_editor`).
   - Enviar resultado para API terceira e finalizar.

### Checklist técnico da implementação (fase 2)

- [ ] Criar módulo de carregamento de fluxo por `flow_uuid` (`flow_v2` + `flow_v2_revision`).
- [ ] Implementar seletor de revisão (publicada maior versão; fallback draft).
- [ ] Definir contrato interno de execução de componente (entrada, saída, status, erro).
- [ ] Criar engine de execução de workflow (iteração por `branches` + resolução de próximo card).
- [ ] Injetar payload de entrada no runtime da sessão logo no início da execução.
- [ ] Implementar `condition`.
- [ ] Implementar `set_variables`.
- [ ] Implementar `api_call` com timeout, retry controlado e registro de resultado.
- [ ] Implementar `code_editor` (execução JS com sandbox e limites).
- [ ] Implementar `scheduling_moment` atualizando `frozen_until`.
- [ ] Implementar `finish_flow` atualizando `ended_at` e finalização da sessão.
- [ ] Atualizar `last_card_uuid` e `next_card_uuid` a cada transição.
- [ ] Registrar warnings/errors em `orch_sessions_alarms` durante execução.
- [ ] Garantir idempotência por sessão/card para evitar dupla execução em corrida.
- [ ] Criar testes automatizados dos 2 fluxos de referência.
- [ ] Criar testes de concorrência para avanço de card e componentes bloqueantes.
- [ ] Medir latência por componente e latência total do fluxo (logs estruturados).

### Checklist de validação funcional (fase 2)

- [x] Fluxo `2cb9482a-131e-4b2a-8507-484745661836` executa ponta a ponta com payload de teste.
- [x] Fluxo `fea492fb-9420-4690-ba09-bd73dca50717` executa com payload de teste e pausa em `scheduling_moment` conforme definição atual.
- [x] `valor_recebido` é injetado no runtime e reutilizado corretamente nos componentes.
- [x] `api_call` realiza postagem esperada para API terceira.
- [x] `code_editor` produz resultado esperado para verificação de número primo.
- [x] `scheduling_moment` congela execução via `frozen_until` quando aplicável.
- [x] `finish_flow` marca `ended_at` e encerra sessão sem regressão de estado.
- [x] Re-trigger em sessão congelada respeita `frozen_until` (não reinicia fluxo e não avança antes do tempo).

### Referência de implementação existente

- Há exemplos na pasta `**docs` de outra aplicação executando os mesmos componentes.
- Usar como base de comparação, ajustando para melhor performance e robustez no `orch`.
- Se faltarem dependências/contexto desses exemplos, levantar explicitamente os gaps para complemento.

### Milestones de execução (fase 2)

#### M1 — Base do motor de workflow

- [x] Implementar carregamento do fluxo por `flow_uuid` em `flow_v2` + `flow_v2_revision`.
- [x] Implementar seleção de revisão (publicada maior versão; fallback draft).
- [x] Definir contrato interno de bootstrap de execução (resultado, motivo, revisão selecionada).
- [x] Implementar engine base de navegação por `branches` (resolução de start/next).
- [x] Injetar payload de entrada no runtime da sessão no início da execução (`input_payload` + `variables`).
- [x] Atualizar `next_card_uuid` durante bootstrap (M1 ainda sem execução real de componentes).
- [x] Validar M1 com testes unitários de seleção de revisão e navegação.

#### M2 — Componentes essenciais + pausa/encerramento

- [x] Implementar `condition` (núcleo de avaliação + branch label).
- [x] Implementar `set_variables` (render simples de `{{variavel}}` + escrita em runtime).
- [x] Implementar `code_editor` (execução JS controlada + branch de saída).
- [x] Implementar `scheduling_moment`/`wait` com atualização de `frozen_until`.
- [x] Implementar `finish_flow` com `ended_at` e finalização de sessão (`state=3`).
- [x] Garantir avanço de cards com atualização transacional de `last_card_uuid` e `next_card_uuid`.
- [x] Normalizar avanço por `ref_id`/`uuid` e persistir cursores de execução no runtime.
- [x] Garantir idempotência por sessão/card para evitar dupla execução (lock transacional por sessão no M2).
- [x] Validar M2 com cenários de bloqueio/desbloqueio e finalização (testes via trigger da API com payload `GenericApp`).

#### M3 — Integrações externas + hardening

- [x] Implementar `api_call` com timeout e registro de resultado (`success/error`).
- [x] Adicionar retry controlado no `api_call` (tentativas limitadas + backoff configurável).
- [x] Implementar `code_editor` (JS) com sandbox e limites.
- [x] Registrar warnings/errors de execução em `orch_sessions_alarms`.
- [x] Executar e validar os 2 fluxos reais:
  - `2cb9482a-131e-4b2a-8507-484745661836`
  - `fea492fb-9420-4690-ba09-bd73dca50717`
- [x] Criar testes de concorrência para avanço de card e eventos muito próximos.
- [x] Medir latência por card e latência total do workflow em logs estruturados e persistir em `orch_session_metrics`.

## Planejamento — Fase 3 (checklist)

### Objetivo da fase 3

Migrar a movimentação de cards do workflow para execução assíncrona com Celery, mantendo a API como ponto de entrada leve (somente “peteleco” inicial), para escalar throughput e desacoplar latência de execução do tempo de resposta HTTP.

### Opinião técnica (Celery Beat)

- Sim, `Celery Beat` é a abordagem correta para este caso.
- Padrão recomendado:
  - API marca sessão como `pending`.
  - Beat agenda tarefa periódica de “dispatcher” (scan de sessões elegíveis).
  - Worker executa sessão card a card até parar/finalizar.
- Vantagem: resolve naturalmente `wait/scheduling` (só avança quando `frozen_until <= NOW()`), e prepara terreno para componentes bloqueantes por evento nas próximas fases.

### Dinâmica alvo da fase 3

1. API recebe `POST /v1/orch/{flow_uuid}`.
2. API persiste/atualiza sessão com `state=0 (pending)` e retorna `202`.
3. Beat dispara dispatcher periódico.
4. Dispatcher seleciona sessões elegíveis para execução.
5. Task de execução avança cards em loop controlado até:
   - `finish_flow` (encerra);
   - `scheduling_moment` (define `frozen_until` e pausa);
   - componente bloqueante por evento (futuro: `waiting`);
   - erro/limite de segurança.

### Regras de elegibilidade (dispatcher)

- Sessão elegível para execução:
  - `state = 0 (pending)`;
  - `next_card_uuid IS NOT NULL` (ou cursor equivalente no runtime);
  - `frozen_until IS NULL OR frozen_until <= NOW()`;
  - não estar finalizada (`state <> 3`).
- Ordenação sugerida:
  - `created_at ASC` (justiça/fairness) ou `updated_at ASC` conforme estratégia.
- Batch configurável por env (ex.: `ORCH_EXECUTION_BATCH_SIZE`).

### Modelo de estados (fase 3)

- `0 = pending` (pronta para execução assíncrona).
- `1 = running` (opcional durante execução do worker; pode ser usado para observabilidade).
- `2 = waiting` (reservado para bloqueio por evento externo — fase 4/5).
- `3 = finished` (encerrada).

> Para `scheduling_moment`, manter `state=0` com `frozen_until` no futuro é suficiente para o filtro do dispatcher.
> Para bloqueio por evento (futuro), o evento externo altera para `state=0` quando for retomar.

### Checklist técnico da implementação (fase 3)

- [x] Adicionar stack Celery (`celery_app`, worker, beat) com configuração por env.
- [ ] Definir filas dedicadas (ex.: `orch_dispatch`, `orch_execute`) e política de retry.
- [x] Implementar task dispatcher periódica (`Celery Beat`) para buscar sessões elegíveis.
- [x] Implementar lock transacional por sessão no dispatcher para evitar dupla enfileiração.
- [x] Implementar task executora por sessão (`session_id`) reaproveitando motor M2 atual.
- [x] Garantir transição de estado consistente (`pending`/`running`/`finished`) sob concorrência.
- [x] Ajustar `POST /v1/orch/{flow_uuid}` para não executar M2 inline (apenas bootstrap + pending), com fallback inline opcional (`CELERY_ENABLED=false`).
- [ ] Manter compatibilidade com `frozen_until` (não avançar antes da janela).
- [ ] Preservar idempotência em re-triggers (mesma sessão ativa deve ser reutilizada).
- [x] Persistir alarmes da esteira assíncrona (`dispatch`/`execute`) em `orch_sessions_alarms`.
- [x] Persistir métricas da esteira assíncrona em `orch_session_metrics` (dispatch lag + execução).
- [x] Adicionar healthchecks mínimos de worker/beat (documentação operacional).
- [ ] Cobrir com testes de integração (API -> pending -> beat/worker -> avanço de cards).

### Checklist de validação funcional (fase 3)

- [x] API responde rápido (`202`) sem executar cards inline.
- [x] Sessão nova entra como `pending` e é capturada pelo dispatcher.
- [x] Fluxo sem wait executa até fim somente via worker.
- [x] Fluxo com `scheduling_moment` pausa e só retoma após `frozen_until`.
- [ ] Re-trigger durante janela de wait não duplica execução.
- [ ] Cenário de alta concorrência não processa mesma sessão em paralelo.
- [ ] Alarmes e métricas refletem ciclo completo (API + dispatch + execute).

### Milestones de execução (fase 3)

#### M1 — Infra assíncrona base

- [x] Estruturar app Celery (`worker` + `beat`) e config por env.
- [x] Criar task dispatcher periódica.
- [x] Criar task executora por sessão.
- [x] Definir lock/idempotência de enfileiramento.

#### M2 — Migração do caminho de execução

- [x] Remover execução inline do M2 no endpoint de trigger (quando `CELERY_ENABLED=true`).
- [x] Ajustar persistência inicial para `pending`.
- [x] Encadear dispatcher -> executor reutilizando motor atual.
- [x] Garantir transições de estado e cursor sem regressão.

#### M3 — Hardening operacional

- [x] Alarmes de falha no dispatcher/executor.
- [x] Métricas de lag de fila e latência fim-a-fim.
- [ ] Testes de carga concorrente com sessões pendentes.
- [x] Runbook de operação (worker down, backlog alto, retries, DLQ se aplicável).

### Variáveis e execução (fase 3)

- Variáveis novas:
  - `CELERY_ENABLED` (recomendado: `true` em ambiente assíncrono)
  - `CELERY_BROKER_URL` (opcional; se ausente, monta via `RABBITMQ_*`)
  - `CELERY_RESULT_BACKEND` (opcional; padrão usa `REDIS_URL`)
  - `CELERY_DISPATCH_INTERVAL_SECONDS` (padrão: `2`)
  - `CELERY_DISPATCH_BATCH_SIZE` (padrão: `100`)
  - `CELERY_DISPATCH_QUEUE` (padrão: `orch_dispatch`)
  - `CELERY_EXECUTE_QUEUE` (padrão: `orch_execute`)
  - `CELERY_HEARTBEAT_QUEUE` (padrão: `orch_heartbeat`)
  - `CELERY_BEAT_DISPATCH_ENABLED` (padrão: `true`; quando `false`, beat envia somente heartbeat)
  - `CELERY_BEAT_RECONCILE_PENDING_EVENTS_ENABLED` (padrão: `true`; ativa reconciliador de eventos de canal pendentes)
  - `CELERY_DISPATCH_WORKSPACE_UUID` (opcional; restringe dispatcher a um workspace específico)
  - `CELERY_RECONCILE_PENDING_EVENTS_WORKSPACE_UUID` (opcional; restringe reconciliador a um workspace específico; fallback para `CELERY_DISPATCH_WORKSPACE_UUID`)
  - `CELERY_TASK_ALWAYS_EAGER` (padrão: `false`; útil para testes locais)
  - `CELERY_HEARTBEAT_KEY` (padrão: `orch:beat:heartbeat`)
  - `CELERY_HEARTBEAT_TTL_SECONDS` (padrão: `30`)
  - `CELERY_RECONCILE_PENDING_EVENTS_INTERVAL_SECONDS` (padrão: `15`)
  - `CELERY_RECONCILE_PENDING_EVENTS_BATCH_SIZE` (padrão: `200`)
  - `CELERY_RECONCILE_PENDING_EVENTS_STALE_SECONDS` (padrão: `30`)
  - `CELERY_RECONCILE_PENDING_EVENTS_COOLDOWN_SECONDS` (padrão: `30`; evita reenfileiramento repetitivo da mesma sessão)
- Subir worker:
  - `celery -A app.core.celery_app:celery_app worker -Q orch_dispatch,orch_execute,orch_heartbeat -l INFO`
- Subir beat:
  - `celery -A app.core.celery_app:celery_app beat -l INFO`

#### Modo controlado para testes exclusivos por workspace

- Definir `CELERY_DISPATCH_WORKSPACE_UUID=<workspace_uuid>` para evitar varredura global.
- Para teste manual sem gerar dispatch automático global:
  - `CELERY_BEAT_DISPATCH_ENABLED=false`
  - executar somente triggers da rota com workspace: `POST /v1/orch/{workspace_uuid}/{flow_uuid}`.

#### Regra operacional de filas para Beat/Tasks

- Tasks de Beat devem usar fila naturalmente relacionada, porém separada da fila de execução principal.
- REGRA PADRÃO DO PROJETO: salvo pedido explícito, nunca reutilizar nomes de filas já existentes em outras aplicações/serviços do ambiente compartilhado.
- Para novos componentes e testes de desenvolvimento, usar nomes isolados por contexto/feature (ex.: `orch_dispatch_f5_local`, `orch_execute_f5_local`, `orch_component_generate_file_run_f5_local`).
- Regra recomendada:
  - dispatch em `orch_dispatch`;
  - execução de sessão em `orch_execute`;
  - heartbeat/rotinas de beat em `orch_heartbeat`.
- Objetivo: evitar ruído operacional, reduzir confusão de suporte e melhorar diagnóstico em produção.

### Healthchecks operacionais (fase 3)

- `GET /health/celery`
  - valida conectividade com broker;
  - valida presença de pelo menos 1 worker (`inspect ping`);
  - valida heartbeat do beat via Redis (`CELERY_HEARTBEAT_KEY`).
- Retornos:
  - `200` quando `broker_ok=true`, `worker_ok=true`, `beat_ok=true`;
  - `503` quando qualquer uma dessas condições falhar.

### Runbook operacional (fase 3)

- **Worker parado**
  - Sintoma: `/health/celery` com `worker_ok=false`.
  - Ação: reiniciar worker (`celery ... worker -l INFO`) e validar health.
- **Beat parado**
  - Sintoma: `/health/celery` com `beat_ok=false` e heartbeat expirado.
  - Ação: reiniciar beat (`celery ... beat -l INFO`).
- **Backlog alto de sessões pendentes**
  - Sintoma: muitas sessões em `state=0` com `next_card_uuid` preenchido.
  - Ação: aumentar paralelismo de worker e/ou reduzir `CELERY_DISPATCH_INTERVAL_SECONDS`.
- **Falhas de enfileiramento/execução**
  - Sintoma: alarmes `workflow_dispatch_*` ou `workflow_execute_task_failed`.
  - Ação: inspecionar `orch_sessions_alarms` e logs de worker; validar broker/redis.
- **Recovery após indisponibilidade**
  - Estratégia: ao voltar worker/beat, o dispatcher retoma sessões `pending` elegíveis automaticamente.

### Sequência manual de subida (pré-systemctl)

Enquanto o `systemctl` não estiver consolidado para todas as fases, usar esta sequência padrão para subir os processos em desenvolvimento.

### Comando operacional rápido: `SUBA_O_AMBIENTE`

Convenção para operação no dia a dia com o agente:

- Ao receber `SUBA_O_AMBIENTE`, o agente deve:
  1. subir a stack completa das fases já homologadas (API + workers + beats);
  2. validar `status` dos processos;
  3. validar filas principais do runtime;
  4. executar 1 smoke real (HTTP/curl) no workspace de teste;
  5. responder “ambiente pronto para testes manuais”.
- Execução padrão em DEV local:
  - `scripts/dev_phase_stack.sh restart`
  - `scripts/dev_phase_stack.sh status`
- Quando solicitado explicitamente:
  - `scripts/launchd_orch.sh restart`
  - `scripts/launchd_orch.sh status`

### Protocolo de retomada entre fases (obrigatório)

Para não perder avanço entre F3 → F4 → F5, a retomada deve ser sempre **encadeada** (fases anteriores em pé).

- Script canônico de DEV local:
  - `scripts/dev_phase_stack.sh start`
  - `scripts/dev_phase_stack.sh status`
  - `scripts/dev_phase_stack.sh smoke 5`
  - `scripts/dev_phase_stack.sh stop`
- O script já sobe API + worker/beat legado + worker FileApp + worker/beat `generate_file`.
- Para FileApp, manter worker dedicado separado do worker legado.
- O script só dispara smoke após validar `workers ready`.
- Em DEV local, os workers sobem com `--without-mingle --without-gossip` para evitar atraso de prontidão em brokers com muitos nós.
- O script usa filas isoladas `*_f5_local` por padrão e workspace escopado em `ba7eb0ec-e565-447c-8c11-8f870cf72a60`.
- Se `orch_generate_file_row_buffer` estiver vazio após smoke:
  - primeiro validar `scripts/dev_phase_stack.sh status`;
  - depois verificar se o flow realmente alcança o card `generate_file` na revisão publicada.
- Se houver intervenção em código/config durante a sessão:
  - reiniciar stack antes de novo teste real (`scripts/dev_phase_stack.sh restart`).

### Política de desenvolvimento vs homologação (obrigatória)

- Desenvolvimento/depuração: usar stack local completa, com todos os processos das fases já homologadas em execução.
- Não usar o servidor de homologação como ambiente de debug contínuo a cada erro pequeno.
- Fluxo recomendado:
  1. corrigir localmente;
  2. subir/reiniciar stack completa local;
  3. validar E2E local;
  4. só então publicar e validar em homolog.
- Objetivo: preservar velocidade de desenvolvimento, evitar confusão de branch/versionamento e reduzir tempo de ciclo.

### Perfil automático de filas por ambiente (obrigatório)

- Variável canônica: `ORCH_QUEUE_PROFILE`.
- Valores aceitos:
  - `auto` (recomendado no `.env`): detecta macOS como `launchd_local` e Linux como `prod`;
  - `launchd_local`;
  - `f5_local`;
  - `prod`.
- Regra prática:
  - manter `ORCH_QUEUE_PROFILE=auto` no `.env` compartilhado;
  - `launchd` e scripts DEV já forçam perfil local;
  - `systemd` força perfil `prod`.
- Se precisar override pontual, usar `CELERY_*_QUEUE` explícitas.

### Serviços persistentes no macOS (`launchd`)

`systemctl` é Linux-only. No macOS, o equivalente é `launchd` com `.plist`.

**Importante (operação atual):**

- Durante desenvolvimento/homologação das fases, o padrão preferencial é `scripts/dev_phase_stack.sh`.
- Quando solicitado explicitamente pelo mantenedor, usar `launchd` (`scripts/launchd_orch.sh`) para operação persistente no macOS.
- Nunca misturar os dois modos ao mesmo tempo.

- Templates: `launchd/*.plist`
- Guia: `launchd/README.md`
- Controle:
  - `scripts/launchd_orch.sh start`
  - `scripts/launchd_orch.sh status`
  - `scripts/launchd_orch.sh restart`
  - `scripts/launchd_orch.sh stop`

Topologia esperada no `launchd`:

- `com.orch.api`
- `com.orch.celery.worker.legacy` (filas `dispatch/execute/heartbeat`)
- `com.orch.celery.worker.fileapp` (filas `FileApp`)
- `com.orch.celery.beat.legacy`
- `com.orch.celery.worker.generate_file`
- `com.orch.celery.beat.generate_file`

Padrão de hostname no Flower (DEV local):

- `orch-celery-worker@_macbook_deivid_dev`
- `orch-celery-fileapp-worker@_macbook_deivid_dev`
- `orch-celery-generate-file-worker@_macbook_deivid_dev`

Observação importante (DEV local):

- A API local deve exportar as mesmas filas locais dos workers (`orch_*_launchd_local`), incluindo:
  - `CELERY_DISPATCH_QUEUE`
  - `CELERY_EXECUTE_QUEUE`
  - `CELERY_HEARTBEAT_QUEUE`
  - `CELERY_S3_FILES_INGEST_QUEUE`
  - `CELERY_SOURCE_LIST_INGEST_QUEUE`
  - `CELERY_FILEAPP_MAILING_ASSOC_QUEUE`
- Se a API publicar em fila diferente do worker local, a sessão fica em `state=0` e o fluxo não avança.

### Serviços persistentes no Linux (`systemd`)

- Arquivos de unit: `systemctl/*.service`
- Serviço dedicado para FileApp: `systemctl/orch-celery-fileapp-worker.service`
- Exemplo de ambiente: `systemctl/orch.env.example`
- Script de operação (instalação/start/stop/status/logs):
  - `scripts/systemd_orch.sh`

Unidades de produção (fase atual):

- `orch-api.service`
- `orch-celery-worker.service`
- `orch-celery-fileapp-worker.service`
- `orch-celery-beat.service`
- `orch-celery-generate-file-worker.service`
- `orch-celery-generate-file-beat.service`

Padrão de hostname no Flower (servidor `10.1.20.136`):

- `orch-celery-worker@136_01`
- `orch-celery-fileapp-worker@136_01`
- `orch-celery-generate-file-worker@136_01`

Regra de filas em produção (importante):

- Em `systemd`, o perfil deve ser sempre `ORCH_QUEUE_PROFILE=prod`.
- Não usar filas locais em produção (`*_launchd_local`, `*_f5_local`, `*_diag*`).
- As units oficiais `systemctl/*.service` já devem subir com perfil `prod`; manter esse padrão evita cruzamento com filas de DEV.

Regra operacional:

- As fases são encadeadas: para evoluir F6/F7/F8, manter F4/F5 de pé durante os testes.
- Após qualquer mudança em API/worker/beat/filas, relançar serviços antes de validar.

#### 0) Pré-requisito

- Ativar ambiente:
  - `source .venv/bin/activate`

#### 1) API (porta 7777)

- `uvicorn app.main:app --host 127.0.0.1 --port 7777`

#### 2) Fase 4 — worker legado (dispatch/execute/heartbeat)

- `celery -A app.core.celery_app:celery_app worker --hostname=orch-celery-worker@136_01 -Q orch_dispatch,orch_execute,orch_heartbeat -l INFO`

#### 2.1) Fase 4/7 — worker FileApp (ingest/process)

- `celery -A app.core.celery_app:celery_app worker --hostname=orch-celery-fileapp-worker@136_01 -Q orch_fileapp_ingest_events,orch_fileapp_source_list_ingest,orch_fileapp_mailing_assoc -l INFO`

#### 3) Fase 4 — beat legado (somente tarefas legadas)

- `CELERY_GENERATE_FILE_ENABLED=false celery -A app.core.celery_app:celery_app beat --schedule=/tmp/orch-celerybeat-legacy-f5 -l INFO`

#### 4) Fase 5 — worker generate_file

- `CELERY_GENERATE_FILE_WORKSPACE_UUID=ba7eb0ec-e565-447c-8c11-8f870cf72a60 celery -A app.core.celery_app:celery_app worker --hostname=orch-celery-generate-file-worker@136_01 -Q orch_component_generate_file_run,orch_component_generate_file_scan -l INFO`

#### 5) Fase 5 — beat generate_file (somente scan do componente)

- `CELERY_BEAT_DISPATCH_ENABLED=false CELERY_BEAT_HEARTBEAT_ENABLED=false CELERY_GENERATE_FILE_WORKSPACE_UUID=ba7eb0ec-e565-447c-8c11-8f870cf72a60 celery -A app.core.celery_app:celery_app beat --schedule=/tmp/orch-celerybeat-generate-file-f5 -l INFO`

#### 6) Observações operacionais importantes

- Manter **dois beats separados**:
  - beat legado (fase 4) sem schedule de `generate_file`;
  - beat `generate_file` (fase 5) sem `dispatch`/`heartbeat`.
- Evita duplicidade de agendamento e ruído no Flower.
- Em testes fora do workspace alvo, ajustar/remover `CELERY_GENERATE_FILE_WORKSPACE_UUID`.

## Política Git de Trabalho (acordo operacional)

Para proteger os resultados já alcançados e profissionalizar a evolução do projeto:

- O agente (Codex) é quem executa os comandos Git no dia a dia.
- Antes de qualquer ação que altere histórico/local/remoto, o agente deve pedir sua confirmação explícita.
- Sem confirmação, o agente não deve executar:
  - criação de branch;
  - commit;
  - push;
  - merge/rebase;
  - tag/release;
  - reset/revert.

### Fluxo padrão por mudança

1. Atualizar `main` local.
2. Criar branch de trabalho (`feat/...`, `fix/...`, `chore/...`).
3. Implementar e validar.
4. Commitar na branch.
5. Push da branch.
6. Abrir PR para `main`.
7. Merge apenas após validação/review.

## Planejamento — Fase 4 (checklist)

### Objetivo da fase 4

Adaptar o `orch` para arquitetura por workspace/schema (`ws_{workspace_uuid}`), removendo dependência de schema fixo por `.env` no caminho principal da API.

### Checklist técnico (fase 4)

- [x] Ajustar endpoint principal para receber `workspace_uuid`:
  - `POST /v1/orch/{workspace_uuid}/{flow_uuid}`
- [x] Manter compatibilidade temporária com rota legada (`/v1/orch/{flow_uuid}`) usando workspace padrão do `.env`.
- [x] Introduzir contexto de workspace/schema por request/task.
- [x] Resolver schema dinamicamente com prefixo `ws_`.
- [x] Validar workspace ativo em `target.workspaces` antes de processar trigger/migrate.
- [x] Implementar controle de migração próprio em `orch_alembic_version` (sem tocar `alembic_version` da outra aplicação).
- [x] Criar endpoint para migrate por workspace:
  - `POST /v1/orch/admin/workspaces/{workspace_uuid}/migrate`
- [x] Criar endpoint para migrate all workspaces ativos:
  - `POST /v1/orch/admin/workspaces/migrate-all`
- [x] Incluir migration incremental para suportar métricas assíncronas (`dispatch`/`executor`).
- [x] Executar migrate-all em todos os workspaces ativos do ambiente-alvo.
- [x] Isolar filas do Celery por responsabilidade (dispatch/execute/heartbeat) para operação clara e suporte em produção.

### Variáveis (fase 4)

- `ORCH_LAB_WORKSPACE_UUID` (LAB atual)
- `ORCH_DEFAULT_WORKSPACE_UUID` (fallback da rota legada)

## Fase 6.1 — New Assign Fields

### Objetivo

Adicionar campos de controle de atribuição na `ws_*.orch_sessions` com migration incremental e idempotente.

### Entrega técnica

- Migration `0007_add_assigned_fields_to_orch_sessions` registrada inicialmente.
- Migration `0009_add_orch_sessions_flow_entity_index` adiciona índice `idx_orch_sessions_flow_entity` em `orch_sessions(flow_uuid, entity)` com tablespace dedicado do workspace (`"<uuid>"`).
- Correção aplicada na migration `0008_fix_assigned_fields_to_timestamps`.
- SQL final esperado:
  - `assigned_at TIMESTAMPTZ NULL`
  - `unassigned_at TIMESTAMPTZ NULL`

### Checklist

- [x] Criar SQL idempotente para `orch_sessions`.
- [x] Registrar migration no pipeline oficial (`migrate-all` / `migrate-workspace`).
- [x] Executar `python -m app.cli migrate-workspace ba7eb0ec-e565-447c-8c11-8f870cf72a60`.
- [x] Aplicar em todos os workspaces ativos (cobertura confirmada: `80/80` com versão `0007`).
- [x] Aplicar correção `0008_fix_assigned_fields_to_timestamps` em todos os workspaces ativos.
- [x] Validar presença das colunas finais em `ws_ba7eb0ec-e565-447c-8c11-8f870cf72a60.orch_sessions`.

## Fase 7 — FileAPP ingest na rota atual

### Objetivo

Migrar o mecanismo de ingestão por evento de arquivo para dentro do `orch`, sem criar nova rota.

### Contrato de entrada (mantido)

- Entrada via rota já existente:
  - `POST /v1/orch/{workspace_uuid}/{flow_uuid}`
- Comportamento especial quando `detect_app(payload) == ArquivosApp`:
  - enfileira pipeline assíncrono de ingestão de arquivo;
  - baixa arquivo via URL do evento (`file.url`) com headers `SYNC_WS_*`;
  - com `mapping_template` (`tipo_1`): popula `persons` e também cria `orch_sessions` por linha;
  - sem `mapping_template` (`tipo_2`): mantém ingestão linha a linha em `orch_sessions`.

### Filas RabbitMQ (isoladas no ORCH)

- `orch_fileapp_ingest_events` (default da aplicação):
  - task de entrada: `app.tasks.fileapp.ingest_event`
- `orch_fileapp_source_list_ingest` (default da aplicação):
  - task de processamento: `app.tasks.fileapp.process_event`

Observação:
- em ambientes locais, usar nomes dedicados por stack (ex.: `*_launchd_local`, `*_f5_local`) para não compartilhar consumo com outras aplicações.

### Observações operacionais

- Não criar endpoint novo para webhook de arquivos nesta fase.
- Decisão explícita:
  - `tipo_1` (`mapping_template` presente): `persons` + `orch_sessions`;
  - `tipo_2` (`mapping_template` ausente): somente `orch_sessions`.

### Regra canônica de decisão (fonte de verdade)

| Condição no evento FileApp | Tipo | Efeito obrigatório |
|---|---|---|
| `mapping_template` presente | `tipo_1` | Persistir em `persons` **e** `orch_sessions` |
| `mapping_template` ausente | `tipo_2` | Persistir **somente** em `orch_sessions` |

Critérios de implementação:
- A decisão deve ser feita no início do fluxo e não pode ser ambígua.
- Não deve haver fallback silencioso de `tipo_1` para `tipo_2`.
- A rota de entrada permanece única: `POST /v1/orch/{workspace_uuid}/{flow_uuid}`.

### Logs obrigatórios (anti-regressão)

Em toda validação de FileApp, deve existir evidência de:
- aceite da API com pipeline:
  - `fileapp_tipo1_ingest` ou `fileapp_tipo2_ingest`;
- ingest enfileirada/recebida no worker;
- processamento finalizado no worker:
  - `fileapp.tipo1.process_event.finished` ou `fileapp.process_event.finished`.

### Definition of Done (FileApp)

`tipo_1` (com `mapping_template`):
- [ ] `202 accepted` com pipeline `fileapp_tipo1_ingest`
- [ ] task de ingest/process concluídas nos logs
- [ ] pelo menos 1 registro correspondente em `orch_sessions`
- [ ] pelo menos 1 registro correspondente em `persons`

`tipo_2` (sem `mapping_template`):
- [ ] `202 accepted` com pipeline `fileapp_tipo2_ingest`
- [ ] task de ingest/process concluídas nos logs
- [ ] pelo menos 1 registro correspondente em `orch_sessions`
- [ ] nenhuma exigência de escrita em `persons`

### Runbook curto de teste E2E (5 passos)

1. Subir stack (modo único: `launchd` **ou** `dev_phase_stack`, nunca ambos).
2. Disparar `curl` na rota oficial com payload de FileApp.
3. Capturar `task_id` da resposta.
4. Validar logs do worker para ingest/process desse `task_id`.
5. Validar SQL no workspace alvo conforme a matriz canônica (`tipo_1`/`tipo_2`).

## Fase 8 — Componentes WhatsApp (modo bloqueante inicial)

### Escopo implementado nesta etapa

Componentes adicionados no motor M2 com comportamento bloqueante:

- `send_with_whatsapp`
- `proccess_whatsapp_response` (compatível também com `process_whatsapp_response`)
- `send_with_dialer`
- `process_dialer_response`

Importante:
- nesta etapa, estes componentes **não** realizam envio efetivo;
- a função é preparar o ponto de retomada do fluxo para etapas futuras da fase.

### Regra de execução (intencional)

Ao encontrar um dos componentes acima durante a execução:

1. interrompe o avanço de cards;
2. persiste `last_card_uuid` como o card atual (bloqueante);
3. persiste `next_card_uuid` com o próximo card do grafo;
4. mantém a sessão em execução (`state=1`, running);
5. não finaliza sessão (`ended_at` permanece `NULL`).

### Sinais de observabilidade esperados

- `stopped_reason=blocked_send_with_whatsapp`
- `stopped_reason=blocked_process_whatsapp_response`
- `stopped_reason=blocked_send_with_dialer`
- `stopped_reason=blocked_process_dialer_response`

Esses motivos devem aparecer nas métricas/logs como parada controlada (não erro).

### Motivação arquitetural

Embora simples, este comportamento é proposital:

- preserva cursor de retomada sem perda de contexto;
- evita avanço automático indevido antes da resposta de canal;
- prepara a evolução da Fase 8 para envio/retorno reais mantendo o motor estável.

## Fase 10 — Associação de mailing no FileApp tipo_1

### Objetivo

No pipeline `fileapp_tipo1` (evento com `mapping_template`), executar a mesma sequência da carga manual via tela no Target Core para evitar lógica paralela no ORCH.

- upload do mailing
- resolução/aplicação de mapping
- import
- associação ao flow

### Regras da chamada

Body obrigatório:

- `mailing_ids_added`: `[<mailing_uuid>]`
- `mailing_ids_removed`: `[]`
- `linked_by`: `file.id` do evento
- `call_origin`: **sempre** `"file_event"`

Headers utilizados:

- `X-WORKSPACE-UUID: <workspace_uuid>`
- `x-application: target`
- `authorization: Bearer <TOKEN>` quando configurado
- fallback de autenticação: `x-api-key` e `x-workspace-api-key` com `target.workspaces.otima_billing_api_key`

### Como o mailing_uuid é resolvido

- no `tipo_1`, o `mailing_uuid` vem da resposta do upload no Target Core.
- sequência canônica executada:
  1. `POST /v2/mailings/upload`
  2. `GET /v2/mailings/mapping-templates`
  3. `GET /v2/mailings/{mailing_id}/field-mappings`
  4. `PATCH /v2/mailings/{mailing_id}` (aplica `mapping_template_id`)
  5. `PUT /v2/mailings/{mailing_id}/field-mappings`
  6. `POST /v2/mailings/{mailing_id}/import`
  7. `POST /v2/flow/{flow_uuid}/mailings`

Regra crítica:
- no passo 5, o status precisa chegar em `READY_TO_INGEST` antes de avançar para import/vínculo.
- no `tipo_1`, o ORCH não deve fazer escrita direta em `orch_sessions`; a carga segue o caminho do Target Core.

## Fase 12 — `proccess_whatsapp_response` com desvio de branch

### Objetivo

Ao receber evento de WhatsApp na rota oficial, usar a sessão corrente (`last_card_uuid`/`next_card_uuid`) e executar o componente `proccess_whatsapp_response`/`process_whatsapp_response` para escolher o branch correto e avançar o fluxo.

### Regra aplicada

- `send_with_whatsapp` continua bloqueante para aguardar retorno do canal.
- quando chega evento WhatsApp com `statuses[].status`, o motor libera o bloqueio WhatsApp da sessão e continua do `next_card_uuid` atual.
- no card `proccess_whatsapp_response`, o branch é decidido por status:
  - `sent` -> `sent`
  - `delivered` -> `delivered`
  - `read` -> `read`
  - `failed` -> `failed`
  - `limit_reached` -> `limit_reached`
- se status não mapeado, segue fallback do grafo (`resolve_next_card_uuid`).

### Observação operacional

- a chegada do evento continua sendo tratada na rota já existente;
- nesta fase, a responsabilidade adicional é somente selecionar o branch do componente de resposta e avançar a execução do fluxo.

### Fila dedicada (visibilidade/retentativa)

- task: `app.tasks.fileapp.associate_mailing`
- fila: `CELERY_FILEAPP_MAILING_ASSOC_QUEUE`
  - `prod`: `orch_fileapp_mailing_assoc`
  - `launchd_local`: `orch_fileapp_mailing_assoc_launchd_local`
  - `f5_local`: `orch_fileapp_mailing_assoc_f5_local`
- atraso inicial configurável antes do vínculo:
  - `CELERY_FILEAPP_MAILING_ASSOC_DELAY_SECONDS` (default `20`)
- antes de vincular (`step 7`), a task consulta `GET /v2/mailings/{mailing_id}` e só segue quando o import estiver pronto (`ingested_at` preenchido ou status final de ingestão).

## Fase 13 — Limites de WhatsApp e consumo por flow

### Novas tabelas por workspace

- `orch_whatsapp_limits`
  - histórico de limites recebidos por telefone;
  - apenas 1 registro ativo por telefone (`in_use=true`);
  - ao chegar novo limite para o mesmo telefone, o anterior vira `in_use=false`.

- `orch_whatsapp_rate_limit_per_flow`
  - consolida consumo diário por `flow_uuid` e `phone`;
  - chave única por (`flow_uuid`, `phone`, `day`);
  - atualizado pelo mecanismo de roteamento do `send_with_whatsapp`.

### Nova rota de limite

- `POST /v1/orch/{workspace_uuid}/whatsapp/limits`
- body:
  - `phone` (string)
  - `allowed_limit` (inteiro >= 0)
- efeito:
  - grava novo evento em `orch_whatsapp_limits` com `received_from_meta_at=NOW()` e `in_use=true`;
  - desativa (`in_use=false`) o limite ativo anterior do mesmo `phone`.

### Integração no `send_with_whatsapp`

- ao preparar `ani` + `linked_actuator=whatsapp` no `contact_list_members`, o ORCH também incrementa `consumed` em `orch_whatsapp_rate_limit_per_flow` para o `flow_uuid`/`phone` do dia corrente.
- regra de saldo:
  - o ORCH avalia os números configurados e tenta escolher um `phone` com limite disponível (`consumed < allowed_limit` do registro `in_use=true`);
  - quando `percentual_consumo > 0` no número configurado, o limite efetivo do dia passa a ser `floor(allowed_limit * percentual_consumo / 100)` para aquele `phone`;
  - se o número inicialmente candidato estiver sem saldo, tenta os demais números configurados;
  - só marca `linked_actuator=whatsapp_without_limit` quando todos os números elegíveis estiverem sem saldo.
  - quando o bloqueio ocorrer por `percentual_consumo` (rate limit efetivo), marca `linked_actuator=whatsapp_without_limit_by_rate_limit`.
- falhas HTTP da API externa fazem retry com backoff no Celery.

### Configuração envolvida

- `SYNC_WEBHOOK_BASE_URL` (base da API Target Core)
- `TARGET_CORE_API_BEARER_TOKEN` (preferencial) ou `SYNC_WEBHOOK_BEARER_TOKEN`
- timeout HTTP reaproveita `SYNC_WS_TIMEOUT_SECONDS`
- `CELERY_FILEAPP_MAILING_ASSOC_DELAY_SECONDS` (retardo anti-corrida entre import e vínculo)
