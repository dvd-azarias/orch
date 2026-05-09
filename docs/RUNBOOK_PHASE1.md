# orch — Runbook operacional (Fase 1)

## 1) Pré-requisitos

- `.venv` ativa
- `.env` configurado (`DATABASE_*` + `DATABASE_SCHEMA`)
- Acesso ao PostgreSQL/PgBouncer

## 2) Migrações SQL

Aplicar no schema da fase 1:

1. `sql/001_create_orch_sessions.sql`
2. `sql/002_add_entity_origin_app.sql`
3. `sql/003_create_orch_sessions_alarms.sql`

## 3) Subida da API

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 7777 --reload
```

## 4) Smoke tests

- `GET /health/live`
- `GET /health/db`
- `GET /health/ready`
- `POST /v1/orch/{flow_uuid}` com payload `GenericApp`
- `GET /v1/orch/sessions/by-flow/{flow_uuid}`
- `GET /v1/orch/alarms?limit=10`

## 5) Diagnóstico rápido

- Erros de payload/cursor devem gerar alarmes em `orch_sessions_alarms`.
- Verificar `X-Request-ID` na resposta e correlacionar com logs JSON.
- Em concorrência alta, validar:
  - sem duplicação indevida de sessão
  - `session_created` alternando `true`/`false` conforme create/reuse

## 6) Testes automatizados

```bash
source .venv/bin/activate
pytest -q tests/test_payload_detection_and_extraction.py \
         tests/test_concurrency_parallel.py \
         tests/test_out_of_order.py \
         tests/test_session_queries.py \
         tests/test_alarms.py
```
