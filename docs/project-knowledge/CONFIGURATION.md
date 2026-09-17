# Configuracao

Somente nomes e semantica sao documentados; valores do `.env` nao fazem parte desta memoria.

## Banco

- `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`.
- `DATABASE_SCHEMA`: fallback legado/health.
- `DATABASE_ECHO`, `DATABASE_USE_NULL_POOL`, `DATABASE_POOL_*`.

## Workflow

- `WORKFLOW_V2_ENABLED`, `WORKFLOW_V2_EXECUTE_M2`.
- `WORKFLOW_CONTEXTUAL_MEMBER_ROUTING_ENABLED`: ativa resolucao fail-closed de `contact_list_members` pelo escopo do payload. Default `false`; exige rollout controlado e restart de API/workers.
- `WORKFLOW_V2_MAX_STEPS`, `WORKFLOW_M2_LOOP_GUARD_REPEAT_THRESHOLD`.
- `WORKFLOW_DIALER_EVENT_CORRELATION_WINDOW_HOURS`.

## Celery e profiles

## Billing legado por sessao

- `ORCH_BILLING_SNAPSHOT_ENABLED=1`: habilita a outbox/publicador legado; default `false` e nao deve ser combinado com o batch novo.
- `ORCH_BILLING_RABBITMQ_URL`: URL AMQP exclusiva ou fallback do broker Celery; nunca registrar a URL completa.
- `ORCH_BILLING_EXCHANGE=domain.events`
- `ORCH_BILLING_ROUTING_KEY=billing.usage.snapshot.v1.target`
- `ORCH_BILLING_APPLICATION_CODE=target`
- `ORCH_BILLING_SERVICE_CODE=service-orch`
- `ORCH_BILLING_METRIC_CODE=service-orch`
- `ORCH_BILLING_PUBLISH_TIMEOUT_SECONDS=3`
- `ORCH_BILLING_PUBLISH_MAX_ATTEMPTS=3`

## Billing batch `service-orch`

- `ORCH_BILLING_ENABLED=false`: feature flag nova; exige `BILLING_RABBITMQ_URL` quando ativa.
- `BILLING_BATCH_SIZE=200`, `BILLING_FLUSH_INTERVAL_SECONDS=300`.
- `BILLING_RETRY_SCAN_INTERVAL_SECONDS=15`, `BILLING_PROCESSING_LEASE_SECONDS=120`.
- `BILLING_RETRY_INITIAL_SECONDS=15`, `BILLING_RETRY_MAX_SECONDS=3600`, `BILLING_RETRY_JITTER_SECONDS=10`.
- `BILLING_RECONCILE_INTERVAL_SECONDS=300`, `BILLING_RECONCILE_LOOKBACK_HOURS=48`.
- `BILLING_REPROCESS_LEASE_SECONDS=3600`, `BILLING_REPROCESS_SCAN_INTERVAL_SECONDS=60`, `BILLING_REPROCESS_CHUNK_SIZE=1000`.
- `BILLING_PUBLISH_CONFIRM_TIMEOUT_SECONDS=10`, `BILLING_PUBLISH_CLAIM_BATCH_SIZE=20`.
- `BILLING_EXCHANGE=domain.events`, `BILLING_ROUTING_KEY=billing.usage.snapshot.v1.target`.
- `BILLING_APPLICATION_CODE=target`, `BILLING_SERVICE_CODE=service-orch`, `BILLING_METRIC_CODE=service-orch`.
- `CELERY_BILLING_QUEUE`: `orch.billing.outbox` em prod, com sufixos isolados nos profiles locais.
- `ORCH_BILLING_ADMIN_CLIENT_ID/SECRET`: autenticacao fail-closed das rotas operacionais.

As flags legada e nova sao mutuamente exclusivas. Detalhes: `docs/BILLING_BATCH_RUNBOOK.md`.

`ORCH_QUEUE_PROFILE` aceita `auto`, `launchd_local`, `f5_local`, `prod`. `auto` escolhe local no macOS e prod nos demais sistemas.

| Fila logica | prod | launchd_local | f5_local |
|---|---|---|---|
| dispatch | `orch_dispatch` | `orch_dispatch_launchd_local` | `orch_dispatch_f5_local` |
| execute | `orch_execute` | `orch_execute_launchd_local` | `orch_execute_f5_local` |
| switch BOT flow | `orch_switch_bot_flow` | `orch_switch_bot_flow_launchd_local` | `orch_switch_bot_flow_f5_local` |
| Supplier V2 Dialer | `orch_dialer_supplier_v2` | `orch_dialer_supplier_v2_launchd_local` | `orch_dialer_supplier_v2_f5_local` |
| Supplier V2 SMS/RCS | `orch_channel_supplier_v2` | `orch_channel_supplier_v2_launchd_local` | `orch_channel_supplier_v2_f5_local` |
| heartbeat | `orch_heartbeat` | `orch_heartbeat_launchd_local` | `orch_heartbeat_f5_local` |
| FileApp ingest | `orch_fileapp_ingest_events` | `orch_fileapp_ingest_launchd_local` | `orch_fileapp_ingest_f5_local` |
| FileApp process | `orch_fileapp_source_list_ingest` | `orch_fileapp_source_list_launchd_local` | `orch_fileapp_source_list_f5_local` |
| mailing assoc | `orch_fileapp_mailing_assoc` | `orch_fileapp_mailing_assoc_launchd_local` | `orch_fileapp_mailing_assoc_f5_local` |
| generate run | `orch_component_generate_file_run` | sufixo launchd | sufixo f5 |
| generate scan | `orch_component_generate_file_scan` | sufixo launchd | sufixo f5 |
| billing | `orch.billing.outbox` | `orch.billing.outbox_launchd_local` | `orch.billing.outbox_f5_local` |

Overrides `CELERY_*_QUEUE` prevalecem. API, publishers e consumers precisam usar os mesmos nomes.

Outras categorias:

- broker/backend: `CELERY_BROKER_URL`, `RABBITMQ_*`, `CELERY_RESULT_BACKEND`, `REDIS_URL`;
- dispatch/heartbeat/reconcile: `CELERY_DISPATCH_*`, `CELERY_BEAT_*`, `CELERY_RECONCILE_*`;
- FileApp: `CELERY_FILEAPP_*`, `CELERY_S3_FILES_INGEST_QUEUE`, `CELERY_SOURCE_LIST_INGEST_QUEUE`;
- generate file: `CELERY_GENERATE_FILE_*`;
- escopo: variaveis `*_WORKSPACE_UUID`.

Defaults importantes:

- Celery desabilitado leva workflow ao modo inline.
- FileApp ingest habilitado, mas depende tambem de Celery.
- heartbeat, dispatch, reconcile de channel events e post-process FileApp sao habilitados por default.
- rescue e hygiene FileApp sao desabilitados por default.
- broker ausente cai em `memory://`, inadequado para processos separados.

## Integracoes

- Files: `ARQUIVOS_*` e `SYNC_WS_*`.
- Target Core: `SYNC_WEBHOOK_BASE_URL`, bearer configuravel e timeout `SYNC_WS_TIMEOUT_SECONDS`.
- Listas de Restrição: `TARGET_CORE_SUPPLIER_API_BASE_URL` deve apontar para o
  processo Supplier, não para o CRUD. A autenticação reutiliza
  `TARGET_CORE_API_BEARER_TOKEN`; timeout, tentativas e backoff usam
  `RESTRICTION_LIST_CHECK_HTTP_TIMEOUT_SECONDS` (default `5`),
  `RESTRICTION_LIST_CHECK_MAX_ATTEMPTS` (default `2`, máximo `5`) e
  `RESTRICTION_LIST_CHECK_RETRY_BACKOFF_SECONDS` (default `0.25`). A URL é
  obrigatória quando o card for usado e exige restart de API/workers.
- Registro de ciclo Dialer Supplier V2: `DIALER_SUPPLIER_V2_ENABLED=false` por
  padrão, com `DIALER_SUPPLIER_V2_WORKSPACE_ALLOWLIST` e
  `DIALER_SUPPLIER_V2_FLOW_ALLOWLIST` obrigatórias quando habilitado. A primeira
  limita também os schemas consultados pelo reconciliador; nunca deixá-la
  global em banco compartilhado. O Gate exige `CELERY_ENABLED=true`, pois a
  chamada externa é deliberadamente feita somente depois do commit.
  Também exige `TARGET_CORE_SUPPLIER_API_BASE_URL` no perfil Supplier e
  `TARGET_CORE_API_BEARER_TOKEN`; ausência impede a inicialização. Timeout,
  tentativas, backoff, lease, intervalo e lote de reconciliação usam
  `DIALER_SUPPLIER_V2_HTTP_TIMEOUT_SECONDS`,
  `DIALER_SUPPLIER_V2_MAX_ATTEMPTS`,
  `DIALER_SUPPLIER_V2_RETRY_BACKOFF_SECONDS`,
  `DIALER_SUPPLIER_V2_REGISTRATION_LEASE_SECONDS`,
  `DIALER_SUPPLIER_V2_RECONCILE_INTERVAL_SECONDS` e
  `DIALER_SUPPLIER_V2_RECONCILE_BATCH_SIZE`. O consumer usa
  `CELERY_DIALER_SUPPLIER_V2_QUEUE`. Habilitar
  `CELERY_BEAT_DIALER_SUPPLIER_V2_RECONCILE_ENABLED=true` em exatamente um Beat
  do ambiente; manter `false` nos demais.
- Múltiplos cards do novo Dialer no mesmo flow permanecem fail-closed por
  padrão. O ORCH exige simultaneamente o Gate Supplier V2 acima e
  `ORCH_DIALER_MULTILANE_V2_ENABLED=true`, além de o flow constar tanto em
  `ORCH_DIALER_MULTILANE_V2_FLOW_UUIDS` quanto em
  `DIALER_SUPPLIER_V2_FLOW_ALLOWLIST`. UUID ausente, inválido, duplicado ou
  fora da interseção impede a inicialização. Os limites locais usam
  `ORCH_DIALER_MULTILANE_V2_MAX_LANES_PER_FLOW` e
  `ORCH_DIALER_MULTILANE_V2_MAX_EXECUTION_GROUPS_PER_FLOW`; com o Gate ligado,
  o primeiro deve ser pelo menos `2` e o segundo não pode excedê-lo. Os
  defaults `false`, lista vazia e limites `1` preservam integralmente flows de
  card único e o Dialer legado. Não habilitar essas flags em produção antes
  dos gates de Kerberos e `service_dialer` e do canário controlado definidos em
  `MULTI_DIALER_EXECUTION_PLAN.md`.
- Dispatch SMS/RCS Supplier V2: `CHANNEL_SUPPLIER_V2_ENABLED=false` por padrão.
  A ativação exige allowlists explícitas em
  `CHANNEL_SUPPLIER_V2_WORKSPACE_ALLOWLIST` e
  `CHANNEL_SUPPLIER_V2_FLOW_ALLOWLIST`, `CELERY_ENABLED=true`, URL/bearer do
  perfil Supplier e uma chave Fernet exclusiva em
  `CHANNEL_SUPPLIER_V2_ENCRYPTION_KEY`. O identificador da chave usa
  `CHANNEL_SUPPLIER_V2_ENCRYPTION_KEY_ID`; a mesma chave/id deve existir no
  Target Core. O registro pós-commit usa a fila exclusiva configurada em
  `CELERY_CHANNEL_SUPPLIER_V2_QUEUE`. O reconciliador do gap commit -> enqueue
  usa `CHANNEL_SUPPLIER_V2_RECONCILE_INTERVAL_SECONDS`,
  `CHANNEL_SUPPLIER_V2_RECONCILE_BATCH_SIZE` e
  `CHANNEL_SUPPLIER_V2_REGISTRATION_LEASE_SECONDS`; habilitar
  `CELERY_BEAT_CHANNEL_SUPPLIER_V2_RECONCILE_ENABLED=true` em exatamente um
  Beat do ambiente. Não habilitar antes da migration, do worker Supplier V2 e
  do canário. Ver `CHANNEL_DISPATCH_V2_PLAN.md`.
- `switch_bot_flow`: `SWITCH_BOT_FLOW_ENABLED`, `TARGET_CORE_API_BASE_URL`, `TARGET_CORE_API_BEARER_TOKEN`, `SWITCH_BOT_FLOW_HTTP_TIMEOUT_SECONDS`, `SWITCH_BOT_FLOW_MAX_ATTEMPTS`, `SWITCH_BOT_FLOW_RETRY_BACKOFF_SECONDS` e `CELERY_SWITCH_BOT_FLOW_QUEUE`. A flag e `false` por default e exige restart de API/worker.
- LLM: `OTIMA_LLM_*`.

## Workspace e docs

- `ORCH_DEFAULT_WORKSPACE_UUID`, `ORCH_LAB_WORKSPACE_UUID`.
- `DOCS_ACCESS_CONTROL_ENABLED`, CIDRs internos/proxies e hosts bloqueados.

## Configuracao efetiva

`UNKNOWN`: valores e overrides de producao, secrets manager, flags de beats, profiles e filas instaladas.
