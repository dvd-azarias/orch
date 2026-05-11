# Fluxo do webhook `/v1/webhook/s3-files`

Documento de referência interna descrevendo tudo o que acontece depois que
recebemos um evento de arquivo no endpoint público
`POST /v1/webhook/s3-files`.

---

## 1. Entrada HTTP e pré-processamento

Arquivo: `app/api/routers/webhook.py`, função `receive_s3_files_webhook`.

1. **Configuração**  
   Recupera `settings = get_settings()`; várias flags controlam o comportamento
   descrito abaixo (`s3_arquivos_app_forward_event`,
   `s3_arquivos_app_forward_url`, `s3_arquivos_app_auto_mailing_enabled`,
   `celery_s3_files_ingest_queue`).

2. **Parse do corpo**  
   - Tenta ler o `payload` recebido no Body; se não estiver presente usa
     `await request.json()`.  
   - Fallback para `await request.body()` em caso de `JSONDecodeError`.

3. **Log estruturado**  
   - Sanitiza todos os headers via `_sanitize_headers` (mascara tokens,
     Authorization etc.).  
   - Escreve log `webhook.s3_files.received` com headers e payload.

4. **Forward opcional**  
   - Se `settings.s3_arquivos_app_forward_event` for `True`, reenviamos o
     payload original para `settings.s3_arquivos_app_forward_url` usando
     `httpx.AsyncClient` (timeout 5 s).  
   - Preserva tipo: se payload é dict vai em `json=`, senão envia como `content`.
   - Falhas geram log `webhook.s3_files.forward_failed`.

5. **Disparo do auto-mailing**  
   - Condição: `settings.s3_arquivos_app_auto_mailing_enabled` e payload é `dict`.  
   - Enfileira a task Celery `ingest_s3_files_event_task` (módulo
     `app/tasks/webhook.py`) na fila `settings.celery_s3_files_ingest_queue`.  
   - Em caso de sucesso loga `webhook.s3_files.auto_mailing_enqueued`.  
   - Exceções registram `webhook.s3_files.auto_mailing_enqueue_failed`.

6. **Resposta HTTP**  
   Sempre retorna `{"status": "ok"}` com HTTP 200; todo processamento pesado é
   assíncrono.

---

## 2. Pipeline Celery

Todas as tasks residem em `app/tasks/webhook.py`.

### 2.1 `ingest_s3_files_event_task`

- **Fila padrão**: `settings.celery_s3_files_ingest_queue`.  
- **Entrada**: payload (dict).  
- **Validações**:
  - Se payload não é `dict`, retorna `{"status": "ignored", "reason": "invalid_payload_type"}`.  
  - Caso contrário agenda `process_s3_files_event_task` na fila
    `settings.celery_s3_files_event_queue`.  
- **Resiliência**: qualquer exceção ao enfileirar gera retry (`self.retry`) com
  backoff manual (`RETRY_DELAYS = 30s, 120s, 300s`).  
- **Log**: `webhook.s3_processing_enqueued`.  
- **Retorno**: status `queued` com `process_task_id`.

### 2.2 `process_s3_files_event_task`

- **Fila**: `settings.celery_s3_files_event_queue`.  
- **Entrada**: payload (dict).  
- **Ação principal**: chama `process_s3_files_event(payload)` (descrita na
  seção 3).  
- **Fluxo de retorno**:
  - Se `process_s3_files_event` não devolve `status == "ready"`, a task apenas
    devolve o dicionário resultante (ignorado ou falha).  
  - Para `status == "ready"`: extrai campos (`workspace_uuid`, `flow_id`,
    `mailing_id`, `internal_mailing_id`, `file_id`) e agenda a task
    `app.tasks.source_list_ingestion.ingest_source_list_task` na fila
    `settings.celery_source_list_ingest_queue`, passando os argumentos
    (`workspace_uuid`, `source_list_id` interno, `flow_id`).  
  - Marca log `webhook.s3_source_list_ingest_enqueued`.  
  - Atualiza monitoria:
    - `S3FilesIngestionCRUD.mark_queued` com `ingest_task_id`.  
    - `WorkspaceS3EventsCRUD.update_event` (`status="queued"`), guardando o id
      da ingest task e metadados do template/flow.

---

## 3. Serviço `process_s3_files_event`

Arquivo: `app/services/s3_files_auto_mailing.py`.

### 3.1 Coleta de contexto

Helpers internos:

| Função | Descrição |
| --- | --- |
| `_extract_payload_file` | Busca o bloco `file` dentro do payload (aceita formatos `payload["file"]` ou `payload["data"]["file"]`). |
| `_extract_key_from_records` | Lê o array `Records` (formato AWS) e extrai `s3.object.key`. |
| `_extract_workspace_from_key` | Resolve `workspace-<uuid>` presente no key. |
| `_ensure_download_url` | Anexa `?download=true` se necessário para baixar via Files App. |

O contexto final (`_resolve_event_context`) inclui: `workspace_uuid`,
`folder_path`, `file_id`, `file_url`, `file_name`, `mime_type`,
`file_size_bytes`, `object_key`, `event_name/timestamp`.

### 3.2 Persistência inicial

1. **Validação mínima**  
   Se faltar `workspace_uuid`, `folder_path`, `file_id` ou `file_url` -> log
   `payload_missing_required_fields` e retorna `status="ignored"`.

2. **Workspace event log**  
   Usa `WorkspaceS3EventsCRUD.create_event` para registrar a ocorrência (tabela
   `ws_<uuid>.workspace_s3_events`). O id retornado (`workspace_event_id`) é
   reaproveitado nos passos seguintes (status `processing`, `queued`, `ready`
   etc).

3. **Idempotência global**  
   - Converte `file_id` para `UUID`; erro => atualiza evento com `status="ignored"` e
     `reason="invalid_file_id"`.  
   - `S3FilesIngestionCRUD.claim_event` garante exclusividade (chave composta
     `workspace_uuid + file_id`).  
   - Se já processado, marca ingestão como ignorada (`duplicate_event`) e
     atualiza `workspace_s3_events`.

### 3.3 Descoberta de trigger

`_find_matching_triggers` consulta `flow_v2` no workspace:

- Filtra flows *ativos*, *não arquivados*, `mode == orchestration`.
- Avalia `canvas_properties.orchestration_trigger.folder_path` e
  `mapping_template_id`.
- Faz `SourceListCRUD.get_template_by_uuid` para obter o template e seu id
  interno.

Resultados:

| Situação | Ação |
| --- | --- |
| Nenhum match | `S3FilesIngestionCRUD.mark_ignored` com `reason="no_matching_flow"` + atualiza workspace event. |
| Mais de um match | Marca `folder_conflict` (impede ambiguidade). |
| Template inválido/ausente | Registra diagnóstico específico (`missing_mapping_template_id`, `invalid_mapping_template_id`, `mapping_template_not_found`). |

### 3.4 Download e criação da source list

Para o único match válido:

1. Atualiza `workspace_s3_events` para `status="processing"` e anexa `flow_id`,
   `mapping_template_id`.  
2. Baixa o arquivo:
   - Usa `download_file_from_files_app` (serviço `app/services/files_app.py`)
     apontando para o Files App.  
   - Grava em arquivo temporário (`tempfile.NamedTemporaryFile`).  
3. Cria upload virtual (`starlette.datastructures.UploadFile`) e chama
   `handle_source_list_upload` com:
   - `mapping_template_id=match.mapping_template_internal_id`  
   - `origin=SourceListOrigin.api`  
   - `description` informativa (`"Carga automática via evento de arquivos ..."`)
4. Tratamento de erros:
   - Exceções => `S3FilesIngestionCRUD.mark_failed` + atualização do evento
     (`status="failed"`, reason `exception`).  
   - Se `handle_source_list_upload` não retorna `Status.ready_to_ingest`, marca
     `mapping_not_ready` (falha).
5. Caso sucesso:
   - Atualiza evento para `status="ready"` + registra `mailing_id` público.  
   - Retorna dicionário com `internal_mailing_id`, `flow_id`, `mapping_template_id`,
     `steps` (logs da pipeline de upload).

### 3.5 Dados retornados ao pipeline Celery

`process_s3_files_event` devolve sempre um dicionário com:

- `status`: `ready`, `failed`, `ignored`.  
- `workspace_uuid`, `file_id`, `flow_id`, `folder_path`.  
- Se `ready`: `mapping_template_id`, `mailing_id` (UUID público),
  `internal_mailing_id` (ID numérico), `steps`.  
- `workspace_event_id`: id do registro criado em `workspace_s3_events`.  
- `diagnostics`: presente nos cenários ignorados.  
- Erros detalhados via `error_detail` (persistidos no CRUD).

Esse dicionário é usado por `process_s3_files_event_task` para decidir se deve
enfileirar a ingestão final.

---

## 4. Componentes auxiliares / dependências

| Componente | Local | Função |
| --- | --- | --- |
| `S3FilesIngestionCRUD` | `app/crud/s3_files_ingestion.py` | Idempotência global (claim), snapshots, marcação de status (`mark_queued`, `mark_failed`, `mark_ignored`). |
| `WorkspaceS3EventsCRUD` | `app/crud/workspace_s3_events.py` | Registro detalhado por workspace (timeline do evento, resultados, erros). |
| `SourceListCRUD` | `app/crud/source_list.py` | Consulta de templates, criação de mailing (via `handle_source_list_upload`). |
| `handle_source_list_upload` | `app/services/source_list_ingestion.py` | Aplica template, gera `source_list`, devolve IDs e passos. |
| `download_file_from_files_app` | `app/services/files_app.py` | Baixa o arquivo original do Files App para o disco do worker. |
| `ingest_source_list_task` | `app/tasks/source_list_ingestion.py` | Task que processa a ingestão do mailing recém-criado (split CSV, map, gerar drafts/membros). |
| Celery Queues | Configuradas em `app/tasks/webhook.py` | `celery_s3_files_ingest_queue` (`ingest_s3_files_event_task`), `celery_s3_files_event_queue` (`process_s3_files_event_task`), `celery_source_list_ingest_queue` (ingestão do mailing). |

---

## 5. Resumo dos principais logs

| Log | Origem | Observação |
| --- | --- | --- |
| `webhook.s3_files.received` | `receive_s3_files_webhook` | Entrada do evento + headers mascarados. |
| `webhook.s3_files.forward_failed` | Endpoint | Erro ao reenviar para endpoint externo configurado. |
| `webhook.s3_files.auto_mailing_enqueued` | Endpoint | Task `ingest_s3_files_event_task` enfileirada. |
| `webhook.s3_processing_enqueued` | Task `ingest_s3_files_event_task` | `process_s3_files_event_task` enfileirada. |
| `s3_auto_mailing.payload_missing_required_fields` | `process_s3_files_event` | Payload insuficiente. |
| `s3_auto_mailing.workspace_event_create_failed` | `process_s3_files_event` | Erro ao registrar evento no workspace. |
| `s3_auto_mailing.mapping_template_not_found` | `_find_matching_triggers` | Configuração inconsistente no flow. |
| `webhook.s3_source_list_ingest_enqueued` | `process_s3_files_event_task` | Ingestão final do mailing agendada. |
| `webhook.s3_workspace_event_update_failed` | Diversos pontos | Alguma atualização em `workspace_s3_events` falhou (investigar logs). |

---

## 6. Referências rápidas

- Endpoint REST: `POST /v1/webhook/s3-files`
- Router: `app/api/routers/webhook.py`
- Tasks: `app/tasks/webhook.py`
- Serviço principal: `app/services/s3_files_auto_mailing.py`
- Ingestão de mailing: `app/tasks/source_list_ingestion.py`
- Configurações relevantes (`settings`):  
  `s3_arquivos_app_forward_event`, `s3_arquivos_app_forward_url`,
  `s3_arquivos_app_auto_mailing_enabled`, `celery_s3_files_ingest_queue`,
  `celery_s3_files_event_queue`, `celery_source_list_ingest_queue`.

---

### Checklist para troubleshooting

1. **Evento chegou?**  
   Verificar `webhook.s3_files.received` (API log) e se houve forward externo.

2. **Task `ingest_s3_files_event_task` foi criada?**  
   Log `webhook.s3_processing_enqueued` + fila `celery_s3_files_ingest_queue`.

3. **`process_s3_files_event_task` rodou?**  
   Conferir status armazenado via `S3FilesIngestionCRUD.get_event_snapshot`.

4. **Trigger encontrado?**  
   Checar `workspace_s3_events` (colunas `diagnostics / orch_import_trigger_result`).

5. **Arquivo baixou / mailing criado?**  
   Logs `s3_auto_mailing.*`, registro na tabela `source_lists` com descrição
   “Carga automática via evento de arquivos ...”.

6. **Ingestão final enfileirada?**  
   Log `webhook.s3_source_list_ingest_enqueued`, task na fila
   `celery_source_list_ingest_queue`.

---

### Código de referência anexado

Os trechos essenciais estão copiados na pasta `.documents/s3_files_webhook_refs/`:

- `receive_s3_files_webhook.py` &rarr; função FastAPI do endpoint.  
- `webhook_tasks.py` &rarr; tasks Celery `ingest_s3_files_event_task` e `process_s3_files_event_task`.  
- `s3_files_auto_mailing.py` &rarr; serviço `process_s3_files_event` com helpers principais.

Utilize esses arquivos quando precisar compartilhar o material técnico em
conjunto com este guia.

---

### Workers Celery necessários

Para que o fluxo funcione ponta a ponta, os seguintes serviços (systemd) devem
estar ativos:

1. **Celery Webhook S3 Ingest Events**  
   - Comando: `celery -A app.tasks.webhook:celery_webhook worker … --queues s3_files_ingest_events`  
   - Responsável por consumir `ingest_s3_files_event_task` e
     `process_s3_files_event_task` (parsing do evento, download do arquivo,
     criação do mailing).

2. **Celery Source List Ingest**  
   - Comando: `celery -A app.tasks.source_list_ingestion:celery_source_list worker … --queues source_list_ingest`  
   - Executa `ingest_source_list_task`, realizando a ingestão completa do mailing
     gerado.

Ambos são necessários: o primeiro prepara o mailing, o segundo finaliza a carga.

---

**Responsável pelo documento:** Codex (gerado automaticamente). Última revisão: 2026-05-11.
