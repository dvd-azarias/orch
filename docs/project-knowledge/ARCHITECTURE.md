# Arquitetura Real

Baseline estatica de 2026-08-24. O estado efetivo de producao permanece separado como `UNKNOWN`.

## Limites do sistema

O ORCH e uma aplicacao Python monolitica em repositorio unico, com tres entrypoints:

- FastAPI em `app/main.py`;
- Celery em `app/core/celery_app.py`;
- CLI de migrations em `app/cli.py`.

A aplicacao nao usa modelos ORM declarativos. Services e repositories executam SQL textual contra PostgreSQL, alterando `search_path` conforme o workspace.

## Camadas observadas

| Camada | Responsabilidade | Referencias |
|---|---|---|
| HTTP | middleware, health, rotas e respostas | `app/main.py`, `app/api/v1/orch.py` |
| Deteccao/extracao | classificar payload e formar identidade | `app/services/app_detector.py`, `app/handlers/` |
| Orquestracao | trigger, bootstrap e M2 | `orch_trigger_service.py`, `workflow_runtime_service.py`, `workflow_m2_service.py` |
| Persistencia | SQL de sessoes, eventos, metricas e workspace | `app/repositories/` |
| Assincrono | tasks, routing e schedules | `app/tasks/`, `app/core/celery_app.py` |
| FileApp | ingest, Target Core e pos-processamento | `fileapp_*`, `file_event_ingest_service.py` |
| Operacao | DEV, launchd e systemd | `scripts/`, `launchd/`, `systemctl/` |

## Caminho HTTP canonico

```text
Request
  -> middleware de docs/request_id
  -> rota /v1/orch/{workspace}/{flow}
  -> workspace ativo e search_path ws_<uuid>
  -> detect_app
  -> caminho FileApp OU process_single_payload
  -> persistencia/correlacao da sessao
  -> bootstrap flow_v2
  -> Celery advance_session OU M2 inline
  -> 202 Accepted
```

O modo inline existe quando `CELERY_ENABLED=false`; portanto o contrato `202` nao garante sempre baixa latencia.

## Arquitetura assincrona

Uma unica app Celery inclui `workflow_tasks`, `fileapp_ingest_tasks` e `generate_file_tasks`. Routing e beat schedule sao construidos no import a partir de settings.

```text
beat workflow -> dispatch queue -> worker workflow -> execute queue -> worker workflow
beat workflow -> heartbeat queue -> worker workflow -> Redis heartbeat
API/FileApp   -> ingest queue -> worker FileApp -> process queue -> worker FileApp
worker FileApp -> mailing assoc queue -> worker FileApp
beat generate -> scan queue -> worker generate -> run queue -> worker generate
```

Os processos sao separados por responsabilidade nos scripts e templates, mas compartilham a mesma app e o mesmo conjunto potencial de schedules. Flags incorretas podem fazer dois beats publicarem a mesma rotina.

## Multi-workspace

- UUID normalizado gera `ws_<uuid>`.
- A rota canonica valida `target.workspaces` e exige `provision_status=completed`.
- A rota legada usa `ORCH_DEFAULT_WORKSPACE_UUID`, depois LAB, depois `DATABASE_SCHEMA`.
- Contexto usa `ContextVar`, mas o isolamento real depende de todo caller aplicar `SET LOCAL search_path` corretamente.
- Advisory locks sao database-wide; alguns usam apenas `session_id`, sem workspace.

## Workflow

M1 seleciona a maior revisao publicada ou draft, injeta runtime e card inicial. M2 relê a revisao corrente, executa sob transacao e advisory lock e persiste cursores apos transicoes.

Componentes observados incluem `set_variables`, `condition`, `code_editor`, `api_call`, `wait/scheduling_moment`, `finish_flow`, `create_contact`, `cache_post`, `cache_get`, `generate_file`, `intelligent_agent`, familias WhatsApp/Dialer e `run_flow`.

Chamadas HTTP, LLM e SFTP podem ocorrer enquanto a transacao da sessao permanece aberta. O efeito externo nao participa do rollback PostgreSQL.

## Evidencia e lacunas

`CONFIRMED`: estrutura por leitura de codigo, SQL, testes, scripts e historico.

`UNKNOWN`: numero real de processos, concorrencia, profiles, overrides, proxy, policies RabbitMQ e versao implantada.

