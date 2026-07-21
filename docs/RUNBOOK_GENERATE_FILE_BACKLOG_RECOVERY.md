# Runbook — Generate File Backlog Recovery

Objetivo: recuperar backlog em `orch_generate_file_row_buffer` sem perda de linhas já enviadas.

## 1) Snapshot inicial

```sql
SELECT now() AS captured_at, status, COUNT(*)
FROM "ws_<workspace_uuid>".orch_generate_file_row_buffer
WHERE job_id = '<job_uuid>'::uuid
GROUP BY status
ORDER BY status;
```

## 2) Pausar novas execuções do job (mantendo ingest)

```sql
UPDATE "ws_<workspace_uuid>".orch_generate_file_job
SET next_run_at = now() + interval '2 hours',
    updated_at = now()
WHERE id = '<job_uuid>'::uuid
RETURNING id, mode, active, next_run_at, updated_at;
```

## 3) Reclassificar falhas recuperáveis para pending

```sql
UPDATE "ws_<workspace_uuid>".orch_generate_file_row_buffer
SET status = 'pending',
    last_error = NULL,
    updated_at = now()
WHERE job_id = '<job_uuid>'::uuid
  AND status = 'failed'
  AND (
      COALESCE(last_error, '') ILIKE '%Arquivo já existe%'
      OR COALESCE(last_error, '') ILIKE '%permission denied%'
      OR COALESCE(last_error, '') ILIKE '%Authentication failed%'
  );
```

## 4) Sanitizar pendências legadas para nome base por carteira (opcional)

Use somente quando o legado tiver nome por telefone e você quiser normalizar para lote por carteira.

```sql
UPDATE "ws_<workspace_uuid>".orch_generate_file_row_buffer
SET payload_jsonb = jsonb_set(
      payload_jsonb,
      '{__destination_config,file_name}',
      to_jsonb(
        CASE
          WHEN COALESCE(payload_jsonb->'__row'->>'Carteira','') <> ''
            THEN ('acan_' || regexp_replace(lower(payload_jsonb->'__row'->>'Carteira'), '[^a-z0-9]+', '_', 'g') || '.csv')
          ELSE 'acan_geral.csv'
        END
      ),
      TRUE
    ),
    updated_at = now()
WHERE job_id = '<job_uuid>'::uuid
  AND status = 'pending';
```

## 5) Retomar execução

```sql
UPDATE "ws_<workspace_uuid>".orch_generate_file_job
SET next_run_at = now(),
    updated_at = now()
WHERE id = '<job_uuid>'::uuid
RETURNING id, next_run_at, updated_at;
```

## 6) Validação pós-recuperação

```sql
SELECT now() AS captured_at, status, COUNT(*)
FROM "ws_<workspace_uuid>".orch_generate_file_row_buffer
WHERE job_id = '<job_uuid>'::uuid
GROUP BY status
ORDER BY status;
```

```sql
SELECT created_at, file_target, rows_selected, rows_sent, result, error_jsonb
FROM "ws_<workspace_uuid>".orch_generate_file_dispatch_audit
WHERE job_id = '<job_uuid>'::uuid
ORDER BY created_at DESC
LIMIT 50;
```
