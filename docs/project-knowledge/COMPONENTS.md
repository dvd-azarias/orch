# Componentes e Responsabilidades

## Processos executaveis

| Componente | Responsabilidade | Entrypoint |
|---|---|---|
| API | rotas, health, request ID, docs ACL | `app.main:app` |
| Worker workflow | dispatch, reconciliacao e execucao de sessao | `app.core.celery_app:celery_app` |
| Beat workflow | agenda heartbeat, dispatch e reconciliacoes | mesma app Celery |
| Worker FileApp | ingest, import, associacao e pos-processamento | mesma app Celery |
| Worker generate_file | scan/run e SFTP | mesma app Celery |
| Beat generate_file | agenda scan | mesma app Celery |
| Worker billing | agrega, publica, reconcilia e reprocessa billing | `app.core.billing_celery_app:billing_celery_app` |
| Beat billing | agenda exclusivamente as rotinas de billing | mesma app Celery dedicada |
| CLI migration | migrate individual ou todos | `python -m app.cli` |

## Modulos principais

- `app/api/v1/orch.py`: contratos HTTP, rota canonica/legada, sessoes manuais, resubmit, aliases, limites e migrations.
- `app/services/orch_trigger_service.py`: fluxo comum, callbacks, descartes, ledger e bootstrap.
- `app/repositories/orch_sessions_repository.py`: maior fronteira SQL; upsert, consultas, cursores, routing e create_contact.
- `app/services/workflow_runtime_service.py`: bootstrap M1.
- `app/services/workflow_m2_service.py`: motor e componentes.
- `app/services/workflow_dispatcher_service.py`: claim e classificacao de parada.
- `app/tasks/workflow_tasks.py`: dispatch, execute, heartbeat e reconciliacao de eventos.
- `app/services/switch_bot_flow_service.py`: resolve/cacheia `runner_token`, valida payload Meta e chama o Runner v5.
- `app/tasks/switch_bot_flow_tasks.py`: relay assincrono e serializado dos eventos de usuario ao BOT.
- `app/tasks/fileapp_ingest_tasks.py`: pipeline FileApp e reconciliadores.
- `app/services/fileapp_tipo1_manual_pipeline_service.py`: sete passos Target Core.
- `app/services/fileapp_mailing_association_service.py`: readiness e vinculo ao flow.
- `app/services/fileapp_processed_file_service.py`: move/reupload/quarentena.
- `app/services/generate_file_dispatch_service.py`: buffer, scan, SFTP e auditoria.
- `app/services/migration_service.py`: registro e aplicacao de SQL versionado.
- `app/services/billing_batch_service.py`: event store, agregacao, outbox, publisher confirm, retry, leases e reprocessamento.
- `app/tasks/billing_batch_tasks.py`: cinco tasks dedicadas de billing.

## Health e observabilidade

- `/health/live`: processo HTTP.
- `/health/db`: `SELECT 1`.
- `/health/ready`: schema default + `orch_sessions`.
- `/health/celery`: broker + pelo menos um worker + heartbeat Redis.
- API: logging JSON e `X-Request-ID`.
- Workers: logging do Celery; nem todos os campos `extra` aparecem no formatter da API.
- Persistencia best-effort: alarmes, metricas e descartes.

## Testes

Em 2026-08-27, a suite coletou 380 casos. Ela mistura unidade, testes com mocks e integracao real com PostgreSQL; 354 passaram e 26 falharam primeiro por testes que ainda chamam `trigger_orch(flow_uuid=...)` depois de a assinatura da rota legada ter mudado para aceitar UUID ou alias. Alguns desses testes, especialmente Dialer, tambem parecem carregar expectativas anteriores ao comportamento atual; nao foi provado que apenas renomear o argumento os deixa verdes.

Nao ha evidencia versionada de uma separacao formal entre suites unitarias e suites DB/E2E.
