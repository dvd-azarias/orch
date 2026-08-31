# Billing batch `service-orch`

Estado: implementado no repositorio, **desligado por default e ainda nao implantado/validado em producao**.

## Contrato faturavel

A unidade faturavel e uma sessao ORCH criada em `orch_sessions`.

- tenant: `workspace_uuid` e respectivo schema `ws_<workspace_uuid>`;
- origem idempotente: `orch_sessions.uuid`;
- instante: `orch_sessions.created_at` (`TIMESTAMPTZ`);
- periodo: primeiro dia do mes UTC de `created_at`;
- metrica/servico: `service-orch` por default.

O produtor sincrono registra somente `orch_billing_events`. Falha nessa gravacao usa savepoint, registra contexto sem segredo e nao derruba a criacao da sessao. O reconciliador consulta `orch_sessions`, fonte canonica, e repara omissoes com a mesma constraint idempotente.

## Legado e ausencia de publicacao dupla

- legado: `ORCH_BILLING_SNAPSHOT_ENABLED=false`;
- batch: `ORCH_BILLING_ENABLED=false`;
- o carregamento de configuracao falha se ambas estiverem `true`;
- `session_service` e o caminho `create_contact` escolhem explicitamente apenas um produtor;
- `orch_billing_usage_snapshots` e o CLI `billing-backfill` continuam legados e nao alimentam o batch;
- nenhuma tabela legada e removida pela migration `0022`.

## Fluxo

```text
nova orch_session
  -> orch_billing_events (pending, idempotente)
  -> billing.aggregate (ate 200 por workspace/periodo/metrica/servico)
  -> orch_billing_snapshots (payload imutavel) + eventos batched, mesma transacao
  -> publish_due imediato no mesmo flush, mais scanner periodico (lease + claim_token)
  -> RabbitMQ publisher confirm + mandatory routing
  -> snapshot sent + eventos sent, mesma transacao
```

`sent` significa que o RabbitMQ confirmou e roteou a mensagem; nao significa que o consumer concluiu o processamento.

Se o processo cair depois do confirm e antes do commit, o lease expira e o mesmo payload e `snapshot_id` sao reenviados. O consumer deve deduplicar por `snapshot_id`.

## Persistencia

Migration: `0022_create_orch_billing_batch_tables`.

- `orch_billing_events`: `pending`, `batched`, `sent`;
- `orch_billing_snapshots`: `pending`, `processing`, `sent`, `failed`, `blocked`;
- `orch_billing_reprocess_requests`: `accepted`, `running`, `completed`, `failed`.

A migration tambem cria `idx_orch_sessions_billing_created_at (created_at, id)`, usado por reconciliacao e reprocessamento. O build desse indice nao e concorrente e pode bloquear writes; medir no LAB e reservar janela/timeout operacional antes de migrar workspaces grandes.

O agregador usa `FOR UPDATE SKIP LOCKED`, ordena por `occurred_at, id` e associa os eventos ao snapshot na mesma transacao. `BILLING_BATCH_SIZE` e maximo, nao minimo: 450 eventos viram `200 + 200 + 50`; 30 ou 1 evento entram no proximo flush.

## Retry e leases

- falha transitoria: `failed`, erro sanitizado, `next_attempt_at`, claim liberado;
- backoff exponencial com teto e jitter; nao existe limite de tentativas;
- ao atingir o teto, o snapshot continua elegivel no intervalo maximo;
- `processing` alem do lease volta automaticamente para `failed`;
- conclusao/falha exige o mesmo `claim_token` que adquiriu o lease;
- payload estruturalmente invalido vai para `blocked`, sem loop infinito.

Reprocessamentos percorrem o mes em chunks persistentes (`cursor_session_id`), default 1000. Cada chunk confirma progresso antes de enfileirar a continuacao; falha nesse enqueue devolve a solicitacao ao scanner. `last_enqueued_at` limita mensagens repetidas enquanto um worker estiver indisponivel.

## Celery dedicado

Aplicacao: `app.core.billing_celery_app:billing_celery_app`.

Fila de producao: `orch.billing.outbox`. Perfis locais usam `orch.billing.outbox_launchd_local` ou `orch.billing.outbox_f5_local`.

Tasks:

- `app.tasks.billing.aggregate`;
- `app.tasks.billing.publish_due`;
- `app.tasks.billing.reconcile`;
- `app.tasks.billing.scan_reprocess`;
- `app.tasks.billing.reprocess`.

Worker: `acks_late=true`, `task_reject_on_worker_lost=true`, prefetch `1`, prefork, concurrency `2`, `max_tasks_per_child=1000`, soft limit `240s`, hard limit `300s`.

Templates genericos, nao instalados:

- `systemctl/orch-celery-billing-worker.service`;
- `systemctl/orch-celery-billing-beat.service`.

Deve existir exatamente uma instancia do Beat de billing por ambiente.

## Configuracao

```env
ORCH_BILLING_SNAPSHOT_ENABLED=false
ORCH_BILLING_ENABLED=false
BILLING_BATCH_SIZE=200
BILLING_FLUSH_INTERVAL_SECONDS=300
BILLING_RETRY_SCAN_INTERVAL_SECONDS=15
BILLING_PROCESSING_LEASE_SECONDS=120
BILLING_RETRY_INITIAL_SECONDS=15
BILLING_RETRY_MAX_SECONDS=3600
BILLING_RETRY_JITTER_SECONDS=10
BILLING_RECONCILE_INTERVAL_SECONDS=300
BILLING_RECONCILE_LOOKBACK_HOURS=48
BILLING_REPROCESS_LEASE_SECONDS=3600
BILLING_REPROCESS_SCAN_INTERVAL_SECONDS=60
BILLING_REPROCESS_CHUNK_SIZE=1000
BILLING_PUBLISH_CONFIRM_TIMEOUT_SECONDS=10
BILLING_PUBLISH_CLAIM_BATCH_SIZE=20
BILLING_RABBITMQ_URL=<amqp-url-segura>
BILLING_EXCHANGE=domain.events
BILLING_ROUTING_KEY=billing.usage.snapshot.v1.target
BILLING_APPLICATION_CODE=target
BILLING_SERVICE_CODE=service-orch
BILLING_METRIC_CODE=service-orch
CELERY_BILLING_QUEUE=orch.billing.outbox
ORCH_BILLING_ADMIN_CLIENT_ID=<id-seguro>
ORCH_BILLING_ADMIN_CLIENT_SECRET=<segredo-seguro>
```

Nunca registrar `BILLING_RABBITMQ_URL` completa.

## API operacional

As duas rotas exigem `x-client-id` e `x-client-secret` dedicados de billing. Se as credenciais nao estiverem configuradas, a autenticacao falha fechada.

### Solicitar reprocessamento

```bash
curl -sS -X POST \
  "http://127.0.0.1:7777/v1/orch/<workspace_uuid>/billing/service-orch/reprocess" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: <uuid>' \
  -H 'X-Requested-By: <identidade>' \
  -H 'x-client-id: <id>' \
  -H 'x-client-secret: <segredo>' \
  -d '{"billing_period":"2026-08","reason":"reconciliacao operacional"}'
```

O request e persistido antes do enqueue e responde `202`. Se o broker Celery falhar, permanece `accepted`; `billing.scan_reprocess` o encontra depois. Repetir workspace + `Idempotency-Key` com o mesmo contrato devolve a mesma solicitacao; reutilizar a chave com periodo, motivo ou solicitante diferente responde `409`.

### Consultar status

```bash
curl -sS \
  "http://127.0.0.1:7777/v1/orch/<workspace_uuid>/billing/service-orch/status?billing_period=2026-08" \
  -H 'x-client-id: <id>' \
  -H 'x-client-secret: <segredo>'
```

Retorna contagens dos tres estados de eventos, cinco estados de snapshots, `quantity_sent`, `oldest_pending_at` e `max_attempt_count`.

## Ordem de implantacao (nao executada)

1. Deploy com legado e batch `false`.
2. No LAB, medir tamanho de `orch_sessions`, tempo/lock do indice `idx_orch_sessions_billing_created_at` e definir `lock_timeout`/`statement_timeout` ou janela de manutencao. Nao executar `migrate-all` cegamente em workspaces grandes.
3. Aplicar `0022` pelos comandos oficiais de migration, primeiro no LAB e depois conforme playbook.
4. Instalar/iniciar o worker dedicado com a flag ainda `false`.
5. Instalar/iniciar exatamente um Beat dedicado com a flag ainda `false`.
6. Confirmar o consumer da fila `orch.billing.outbox` e a binding da routing key.
7. Confirmar `ORCH_BILLING_SNAPSHOT_ENABLED=false`.
8. Definir `ORCH_BILLING_ENABLED=true` e `BILLING_RABBITMQ_URL` segura.
9. Reiniciar API/produtores, worker e Beat de billing.
10. Smoke sem eventos: nenhuma publicacao inesperada.
11. Criar uma sessao real controlada e observar `pending -> batched -> sent` em um flush; o agregador chama a publicacao imediatamente, sem somar o tick de retry de 15s.
12. Confirmar publisher confirm, roteamento e ausencia de publicacao legada.
13. Somente depois solicitar reprocessamentos historicos.
14. Monitorar estados, tentativas, `next_attempt_at`, leases e erros bloqueados.

## Rollback nao destrutivo

1. Definir `ORCH_BILLING_ENABLED=false`.
2. Reiniciar API/produtores, worker e Beat de billing.
3. Nao apagar eventos, snapshots nem solicitacoes.
4. Preservar estado para diagnostico e retomada.
5. Nao reativar automaticamente `ORCH_BILLING_SNAPSHOT_ENABLED`.
6. Reativar legado somente por decisao operacional explicita.

## Validacao e UNKNOWN

Confirmado em desenvolvimento:

- testes de contrato de locking, flags, payload, headers, retry, lease, API e reprocessamento chunked;
- PostgreSQL real com tabelas temporarias: 450 sessoes -> eventos -> snapshots `200 + 200 + 50`.
- migration exata executada em schema descartavel dentro de transacao revertida;
- 157 testes focados verdes; suite completa com 415 verdes e os mesmos 27 failures preexistentes do baseline;
- stack local completa `up` e smoke dos dois flows homologados com `202 accepted`, mantendo billing desligado.

`UNKNOWN` ate homologacao controlada:

- binding/DLQ/policies e backlog do RabbitMQ alvo;
- publisher confirm e retorno `mandatory` observados no broker alvo;
- consumo/deduplicacao observado no consumer real;
- units e valores efetivos do ambiente implantado;
- smoke E2E em workspace real apos a migration.
- duas transacoes PostgreSQL concorrentes disputando os mesmos eventos/snapshots.
