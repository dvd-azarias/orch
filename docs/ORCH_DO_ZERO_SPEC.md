# ORCH - Especificacao Completa para Rebuild do Zero

## 1. Objetivo deste documento

Este documento define, com alto nivel de detalhe, o comportamento funcional, tecnico e operacional que deve ser preservado para reconstruir o ORCH do zero sem perda de funcionalidades.

Escopo:
- Entrada de eventos externos (webhooks/payloads diversos).
- Gerenciamento de sessoes por workspace/schema.
- Execucao de workflow Canvas (flow v2).
- Execucao assincrona com Celery.
- Integracoes FileApp e WhatsApp.
- Observabilidade, operacao e validacao de paridade.

Fontes canonicas de referencia:
- `AGENTS.md`
- `README.md`
- `docs/MIGRATIONS_PLAYBOOK.md`

## 2. Definicao do produto ORCH

ORCH e um motor de orquestracao orientado a eventos externos que:
- recebe payloads heterogeneos;
- identifica a origem do evento;
- extrai chave logica de sessao;
- cria/reusa sessao de forma transacional;
- carrega e executa fluxo desenhado no Canvas;
- desacopla resposta HTTP da execucao via esteira assincrona;
- expõe diagnostico operacional por health, alarmes e metricas.

## 3. Contrato de entrada e rotas

Rota principal (atual):
- `POST /v1/orch/{workspace_uuid}/{flow_uuid}`

Compatibilidade legada:
- `POST /v1/orch/{flow_uuid}` usando workspace default.

Rotas de operacao/consulta (ja previstas no comportamento existente):
- `GET /health/live`
- `GET /health/db`
- `GET /health/ready`
- `GET /health/celery`
- `GET /v1/orch/sessions/{session_uuid}`
- `GET /v1/orch/sessions/by-flow/{flow_uuid}`
- `GET /v1/orch/sessions/by-entity?...`
- `GET /v1/orch/alarms?...`
- `POST /v1/orch/{workspace_uuid}/{flow_uuid}/sessions`
- `POST /v1/orch/{workspace_uuid}/{flow_uuid}/sessions/unassign`
- `POST /v1/orch/{workspace_uuid}/whatsapp/limits`

Requisito de resposta para trigger principal:
- retorno rapido `202 Accepted`;
- incluir metadados de aceite e rastreabilidade (`api_version`, identificadores, estado, etc.);
- nao bloquear HTTP esperando workflow completo.

## 4. Modelo de dados obrigatorio

Tabela central: `orch_sessions` (por schema `ws_{workspace_uuid}`).

Campos fundamentais:
- Identidade: `id`, `uuid`, `flow_uuid`
- Estado/tempo: `state`, `started_at`, `ended_at`, `abandoned_at`, `frozen_until`
- Chave de entidade: `entity`, `entity_type`, `entity_address`, `entity_session_id`
- Origem: `entity_origin_app`
- Cursor de execucao: `last_card_uuid`, `next_card_uuid`
- Runtime: `runtime_variables`, `agent_interactions`
- Atribuicao: `assigned_at`, `unassigned_at`
- Timestamps de canal (WhatsApp/Dialer)
- Auditoria: `created_at`, `updated_at`

Estados canonicos:
- `0 = pending`
- `1 = running`
- `2 = waiting`
- `3 = finished`
- `5 = stopped_after_unassign`

Regra de sessao ativa:
- Ativa se `state <> 3 AND unassigned_at IS NULL`.

Chave logica de reuso:
- `flow_uuid + entity + entity_type + entity_address`.

Tabelas auxiliares obrigatorias:
- `orch_sessions_alarms`
- `orch_session_metrics`
- `orch_discarded_events`
- `orch_whatsapp_limits`
- `orch_whatsapp_rate_limit_per_flow`

## 5. Deteccao de app e extracao de payload

Ordem de deteccao obrigatoria:
1. `ArquivosApp`
2. `WhatsApp`
3. `DialerApp`
4. `GenericApp`

### 5.1 ArquivosApp
Sinais:
- `file`
- `file.id`
- `file.original_name`
- `file.folder_path`
- eventos S3/MinIO (`EventName`, `Records[].eventSource`)

Mapeamento base:
- `entity = file.id`
- `entity_type = file`
- `entity_address = file.folder_path + "/" + file.original_name`
- `entity_session_id = file.id`

### 5.2 WhatsApp
Sinais:
- `object = whatsapp_business_account`
- `entry[].changes[].value.messaging_product = whatsapp`
- `statuses[]` e/ou `contacts[].wa_id`

Mapeamento base:
- `entity = contacts[0].wa_id` ou `statuses[0].recipient_id`
- `entity_type = person`
- `entity_address = mesmo identificador`
- `entity_session_id = mesmo identificador`

Status esperados:
- `sent`
- `delivered`
- `read`
- `failed`
- `limit_reached` (fase avancada de branch)

### 5.3 DialerApp
Sinais:
- `hangup`
- `makecall`
- `uniqueid`

Mapeamento base:
- `entity = identificador disponivel`
- `entity_type = person`
- `entity_address = telefone extraido`
- `entity_session_id = uniqueid/hangup.Uniqueid/hangup.Linkedid`

### 5.4 GenericApp
Criterio:
- payload nao classificado nas apps anteriores.

Mapeamento base:
- `entity = external_id` (ou `generated-<uuid4>`)
- `entity_type = api_request`
- `entity_address = entity`
- `entity_session_id = entity`

Observacao critica:
- `entity_origin_app` representa a origem inicial da sessao (historico).
- Origem do evento corrente deve ser derivada do runtime (`runtime_variables.source_app`, snapshots do ultimo payload/extracao).

## 6. Persistencia transacional e concorrencia

Requisitos:
- lock transacional por chave logica (`pg_advisory_xact_lock`);
- evitar duplicacao sob concorrencia alta e eventos fora de ordem;
- reaproveitar sessao mais recente quando aplicavel antes de criar nova;
- atualizacao de cursor e estado sempre no mesmo contexto transacional.

Comportamento esperado:
- se existe sessao ativa para a chave, atualizar;
- senao, criar nova com `started_at` e estado coerente;
- timestamps por status de canal devem ser atualizados de forma idempotente.

## 7. Motor de workflow Canvas (flow v2)

Fontes:
- `flow_v2`
- `flow_v2_revision`

Selecao de revisao:
- maior versao publicada;
- fallback para `draft` se nao houver publicada.

### 7.1 Bootstrap (M1)
- carregar revisao;
- injetar payload no runtime;
- resolver card inicial;
- definir `next_card_uuid`;
- retornar metadados de bootstrap.

### 7.2 Execucao de componentes (M2/M3)
Componentes preservados:
- `condition`
- `set_variables`
- `code_editor` (JS sandboxado)
- `api_call` (timeout + retry/backoff)
- `scheduling_moment` / `wait`
- `finish_flow`

Regras de cursor:
- a cada transicao, persistir `last_card_uuid` e `next_card_uuid`;
- nao perder cursor em falha/interrupcao;
- idempotencia por sessao/card em corrida.

Regras de parada:
- `wait/scheduling`: pausar com `frozen_until`;
- bloqueio por canal: pausar mantendo contexto;
- `finish_flow`: `ended_at` + `state=3`.

## 8. Arquitetura assincrona com Celery

Objetivo:
- API apenas aceita e dispara esteira, sem executar todo fluxo inline.

Filas por responsabilidade:
- `orch_dispatch`
- `orch_execute`
- `orch_heartbeat`
- filas dedicadas para FileApp e associacao de mailing.

### 8.1 Beat/Dispatcher
- agendamento periodico;
- varredura de sessoes elegiveis:
  - `state=0`
  - `next_card_uuid IS NOT NULL`
  - `frozen_until IS NULL OR frozen_until <= NOW()`
- lock para evitar dupla enfileiracao.

### 8.2 Executor
- executa sessao card a card;
- atualiza estado, cursor, metricas e alarmes;
- respeita pausas/bloqueios/finalizacao.

### 8.3 Health operacional
`GET /health/celery` deve validar:
- broker conectado;
- pelo menos 1 worker ativo (`inspect ping`);
- heartbeat do beat (chave Redis com TTL valido).

Status:
- `200` somente se tudo estiver OK;
- `503` quando qualquer dependencia critica falhar.

## 9. Multi-workspace e schema dinamico

Regras obrigatorias:
- cada workspace em schema `ws_{workspace_uuid}`;
- validar workspace ativo antes de processar trigger/migrate;
- migrations por workspace;
- controle de versao de migration do ORCH isolado (`orch_alembic_version`).

Comandos padrao:
- `python -m app.cli migrate-all`
- `python -m app.cli migrate-workspace <workspace_uuid>`

## 10. FileApp: regras canonicas

Entrada continua na rota principal:
- `POST /v1/orch/{workspace_uuid}/{flow_uuid}`

Decisao de tipo e somente por `mapping_template`:
- `tipo_1` (com template): caminho com persistencia em `persons` e `orch_sessions` (ou fluxo equivalente via Target Core conforme fase vigente);
- `tipo_2` (sem template): persistencia somente em `orch_sessions`.

Proibicao:
- nao criar endpoint paralelo para FileApp.

Evidencia E2E minima:
1. `202 accepted` com `pipeline` correto;
2. task de ingest recebida/enfileirada;
3. task de processamento concluida;
4. SQL comprovando persistencia conforme tipo.

Log de diagnostico obrigatorio:
- `decision=fileapp_tipo1` ou `decision=fileapp_tipo2`.

## 11. Associacao de mailing (FileApp fase complementar)

No caminho `tipo_1`:
- associacao ao flow deve ser assincrona via Celery;
- nao bloquear processamento local.

Chamada esperada:
- `POST {SYNC_WEBHOOK_BASE_URL}/v2/flow/{flow_uuid}/mailings`

Body obrigatorio:
- `mailing_ids_added` com mailing resolvido;
- `mailing_ids_removed = []`;
- `linked_by = file.id`;
- `call_origin = "file_event"` (sempre).

Gating de status:
- so enfileirar associacao quando source list estiver `READY_TO_INGEST`.

Restricoes:
- nao usar `UPLOADED` como criterio para associar;
- nao manipular `source_list_members` localmente nesse fluxo.

## 12. WhatsApp: bloqueio, branch e limites

### 12.1 Bloqueio e retomada
- `send_with_whatsapp` bloqueia aguardando retorno do canal;
- na chegada de evento WhatsApp, libera bloqueio e continua a partir de `next_card_uuid`.

### 12.2 Branch por status no `process_whatsapp_response`
Mapeamento:
- `sent -> sent`
- `delivered -> delivered`
- `read -> read`
- `failed -> failed`
- `limit_reached -> limit_reached`

Fallback:
- se status nao mapeado, usar regra padrao de navegacao do grafo.

### 12.3 Limites por telefone/flow
Rota:
- `POST /v1/orch/{workspace_uuid}/whatsapp/limits`

Entrada:
- `phone`
- `allowed_limit` (>= -1, sendo `-1` ilimitado)

Efeito:
- normalizar telefone para chave canonica;
- manter historico em `orch_whatsapp_limits`;
- apenas 1 registro ativo (`in_use=true`) por telefone.

Consumo:
- incrementar por `flow_uuid + phone + day` em `orch_whatsapp_rate_limit_per_flow`;
- aplicar regra de percentual por numero configurado;
- classificar bloqueios sem saldo com atuadores especificos de diagnostico.

## 13. Observabilidade e padrao de erro

Obrigatorio:
- logging estruturado JSON (`request_id`, path, status, latencia);
- aceitar e devolver `X-Request-ID`;
- persistencia best-effort de alarmes e descartes;
- resposta de erro padronizada com `api_version`, `code`, `detail`, `request_id`.

Objetivo:
- diagnosticar corrida, backlog, falha externa e regressao de fluxo sem depender de debug ad-hoc.

## 14. Operacao e runtime local/producao

Padrao DEV local:
- `scripts/dev_phase_stack.sh` (`start`, `status`, `smoke`, `stop`, `restart`)

Regra de ouro:
- nao misturar `launchd` e stack manual ao mesmo tempo.

Apos alteracao de runtime (API/worker/beat/filas):
- reiniciar servicos antes de validar.

Filas:
- usar isolamento por ambiente/perfil para evitar ruido cruzado;
- preservar `ORCH_QUEUE_PROFILE` (`auto`, `launchd_local`, `f5_local`, `prod`);
- evitar reutilizar fila compartilhada de outro servico sem solicitacao explicita.

Hostnames de workers:
- explicitos para facilitar filtro/diagnostico no Flower.

## 15. Blueprint de rebuild do zero (ordem recomendada)

1. Fundacao
- FastAPI + SQLAlchemy + Celery + Redis + RabbitMQ;
- contexto de workspace/schema por request/task.

2. Banco
- criar `orch_sessions` e auxiliares;
- indices e constraints;
- pipeline de migration por workspace.

3. Entrada HTTP
- rota principal + health + consultas operacionais;
- detector/extrator por app.

4. Sessao transacional
- lock por chave logica;
- create/reuse com idempotencia;
- atualizacao de estado e cursores.

5. Workflow engine
- loader de revisao;
- bootstrap M1;
- execucao M2/M3 de componentes.

6. Esteira assincrona
- beat dispatcher + worker executor;
- heartbeats e health de Celery;
- metricas e alarmes de dispatch/execute.

7. FileApp
- pipeline `tipo_1` e `tipo_2`;
- filas dedicadas;
- associacao de mailing assincrona.

8. WhatsApp avancado
- bloqueio/retomada;
- branch por status;
- limites por telefone/flow/dia.

9. Endurecimento operacional
- logs estruturados;
- runbook de falhas;
- smoke e validacao automatizada.

## 16. Checklist de paridade funcional (gate de go-live)

- Trigger principal retorna `202` sem bloquear workflow completo.
- Sessao e criada/reusada com chave logica correta.
- Regra de sessao ativa preservada (`state <> 3 AND unassigned_at IS NULL`).
- Cursores (`last_card_uuid`/`next_card_uuid`) sempre consistentes.
- `wait` respeita `frozen_until`.
- Bloqueios de canal pausam e retomam corretamente.
- Dispatcher nao duplica execucao em concorrencia.
- FileApp decide tipo apenas por `mapping_template`.
- Associacao de mailing ocorre de forma assincrona com `call_origin=file_event`.
- Limites WhatsApp aplicam saldo por `flow + phone + day`.
- Alarmes/metricas/health oferecem evidencia objetiva de runtime.

## 17. Criterio de aceite final

Um rebuild so e considerado fiel ao ORCH quando:
- contratos de API e semantica de sessao forem equivalentes;
- execucao Canvas em modo assincrono mantiver idempotencia e cursores;
- regras de FileApp e WhatsApp avancadas forem preservadas;
- observabilidade e operacao forem suficientes para suporte em producao;
- testes e evidencias E2E demonstrarem paridade real, nao apenas compilacao.
