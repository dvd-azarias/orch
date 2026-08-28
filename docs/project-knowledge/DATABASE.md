# Banco de Dados

## Conexao e contexto

- SQLAlchemy async + `asyncpg`.
- `NullPool` e o default; pool local e configuravel.
- Cada request/task seleciona `ws_<workspace_uuid>` via `search_path`.
- A dependency HTTP commita ao final e faz rollback em excecao.
- Nao existem models ORM declarativos; o schema real esta em SQL textual e migrations.

## Objetos ORCH

| Objeto | Escopo | Papel |
|---|---|---|
| `orch_sessions` | workspace | sessao, estado, cursores, runtime e timestamps de canal |
| `orch_sessions_alarms` | workspace | alarmes operacionais |
| `orch_session_metrics` | workspace | metricas de workflow/dispatch/executor |
| `orch_discarded_events` | workspace | auditoria de eventos ignorados |
| `orch_channel_events` | workspace | ledger de eventos de canal |
| `orch_generate_file_*` | workspace | jobs, buffer e auditoria de arquivos |
| `orch_whatsapp_limits` | workspace | historico de limite por telefone |
| `orch_whatsapp_rate_limit_per_flow` | workspace | consumo diario por flow/telefone |
| `orch_billing_usage_snapshots` | workspace | outbox unitario legado, desligado por default |
| `orch_billing_events` | workspace | event store idempotente do billing batch |
| `orch_billing_snapshots` | workspace | outbox agregado, leases, retry e payload imutavel |
| `orch_billing_reprocess_requests` | workspace | auditoria de reprocessamento operacional |
| `orch_alembic_version` | workspace | controle de migrations do ORCH |
| `target.orch_flow_aliases` | central | alias curto para workspace/flow |

## Objetos compartilhados

- Leitura: `target.workspaces`, `flow_v2`, `flow_v2_revision`.
- Leitura/escrita em fluxos especificos: `source_lists`, `persons`, `contact_list_members`, `cache_card_store`.
- Ownership confirmado do Target Core: `contact_list_members` e `linked_actuator_enum`.
- Ownership formal de `source_lists` e `persons`: `UNKNOWN`, apesar de haver escrita ORCH no componente `create_contact`.
- `source_list_members`: nao referenciada pelo codigo FileApp.

## Migrations

Lista executavel: `0001` a `0015`, depois `0018` a `0022`.

- `0016/0017` permanecem como arquivos historicos, mas foram retiradas do pipeline porque alteravam enum de outro sistema.
- Todas as pendencias de um workspace rodam numa transacao.
- `migrate-all` percorre workspaces `completed` sequencialmente; falha interrompe os seguintes, sem reverter workspaces ja concluidos.
- Nao ha checksum, head unico, lock de migracao ou detector de drift de arquivo.
- `0022` cria `idx_orch_sessions_billing_created_at (created_at, id)` para evitar full scan do billing. Como o pipeline e transacional, o build nao usa `CONCURRENTLY` e exige medicao no LAB/janela operacional para workspaces grandes.
- O parser SQL e simples e nao suporta genericamente dollar-quoted blocks.
- Paths de SQL sao relativos ao diretorio de execucao.

CLI individual deriva o schema do UUID sem consultar elegibilidade do workspace. O endpoint HTTP individual valida `completed` ou `running/orch_migrate`.

## Locks e concorrencia

- Upsert de sessao: `pg_advisory_xact_lock(hashtext(chave logica))`.
- Executor M2: `pg_try_advisory_xact_lock(92021, session_id)`.
- Dispatcher, ledger e generate-file: `FOR UPDATE SKIP LOCKED`.
- Routing WhatsApp: serializacao por flow.
- Alguns advisory locks nao incluem workspace; IDs iguais em schemas distintos compartilham namespace no mesmo database.

## Transacoes perigosas

- Dispatcher lista workspaces, abre transacao implicita, faz claims em savepoints e fecha a sessao sem commit explicito. Enqueue Celery permanece externo enquanto claim/metricas podem ser revertidos.
- API pode publicar task antes do commit final do request.
- M2 mantem transacao durante HTTP/LLM; generate-file durante SFTP.
- Efeitos externos nao possuem atomicidade com PostgreSQL.

## Estado desconhecido

- Versoes aplicadas por workspace e drift real.
- Versoes orfas `0016/0017` em `orch_alembic_version`.
- Permissoes, tablespaces e extensoes no ambiente implantado.
- Backlog, volume e qualidade dos dados.
