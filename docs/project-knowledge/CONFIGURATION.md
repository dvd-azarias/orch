# Configuracao

Somente nomes e semantica sao documentados; valores do `.env` nao fazem parte desta memoria.

## Banco

- `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`.
- `DATABASE_SCHEMA`: fallback legado/health.
- `DATABASE_ECHO`, `DATABASE_USE_NULL_POOL`, `DATABASE_POOL_*`.

## Workflow

- `WORKFLOW_V2_ENABLED`, `WORKFLOW_V2_EXECUTE_M2`.
- `WORKFLOW_V2_MAX_STEPS`, `WORKFLOW_M2_LOOP_GUARD_REPEAT_THRESHOLD`.
- `WORKFLOW_DIALER_EVENT_CORRELATION_WINDOW_HOURS`.

## Celery e profiles

`ORCH_QUEUE_PROFILE` aceita `auto`, `launchd_local`, `f5_local`, `prod`. `auto` escolhe local no macOS e prod nos demais sistemas.

| Fila logica | prod | launchd_local | f5_local |
|---|---|---|---|
| dispatch | `orch_dispatch` | `orch_dispatch_launchd_local` | `orch_dispatch_f5_local` |
| execute | `orch_execute` | `orch_execute_launchd_local` | `orch_execute_f5_local` |
| heartbeat | `orch_heartbeat` | `orch_heartbeat_launchd_local` | `orch_heartbeat_f5_local` |
| FileApp ingest | `orch_fileapp_ingest_events` | `orch_fileapp_ingest_launchd_local` | `orch_fileapp_ingest_f5_local` |
| FileApp process | `orch_fileapp_source_list_ingest` | `orch_fileapp_source_list_launchd_local` | `orch_fileapp_source_list_f5_local` |
| mailing assoc | `orch_fileapp_mailing_assoc` | `orch_fileapp_mailing_assoc_launchd_local` | `orch_fileapp_mailing_assoc_f5_local` |
| generate run | `orch_component_generate_file_run` | sufixo launchd | sufixo f5 |
| generate scan | `orch_component_generate_file_scan` | sufixo launchd | sufixo f5 |

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
- LLM: `OTIMA_LLM_*`.

## Workspace e docs

- `ORCH_DEFAULT_WORKSPACE_UUID`, `ORCH_LAB_WORKSPACE_UUID`.
- `DOCS_ACCESS_CONTROL_ENABLED`, CIDRs internos/proxies e hosts bloqueados.

## Configuracao efetiva

`UNKNOWN`: valores e overrides de producao, secrets manager, flags de beats, profiles e filas instaladas.

