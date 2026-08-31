# Integracoes Externas

## PostgreSQL / Target data plane

O ORCH usa o mesmo database para schemas `ws_*` e objetos centrais `target.*`. Le dados de workspace e flow e, em componentes especificos, altera tabelas compartilhadas.

Risco: ownership parcial e acoplamento de schema. `linked_actuator_enum` e explicitamente responsabilidade do Target Core.

## RabbitMQ e Redis

RabbitMQ transporta tasks Celery. Redis e usado como backend opcional, heartbeat do beat e locks/cooldowns de reconciliacao.

## Billing snapshots legados

Quando `ORCH_BILLING_SNAPSHOT_ENABLED=1`, cada nova sessao local cria uma outbox idempotente por `snapshot_id`. O beat publica o snapshot apos o commit no exchange topic `domain.events`, usando a routing key oficial `billing.usage.snapshot.v1.target`. Falha do broker nao interrompe a criacao da sessao; o item permanece pendente para nova tentativa.

Para retroativos, usar `python -m app.cli billing-backfill --period YYYY-MM --dry-run` antes de inserir lotes limitados. O comando cria somente sessoes sem registro de outbox no mesmo workspace, deriva o envelope do `created_at` em UTC e usa bloqueio `SKIP LOCKED`; portanto, pode ser repetido sem criar uma segunda cobranca. A opcao `--rearm-exhausted` reabilita apenas snapshots pendentes sem publicacao cujas tentativas ja atingiram o limite configurado.

`UNKNOWN`: policies, DLQ, durabilidade, bindings, consumidores e backlogs reais.

## Billing batch `service-orch`

O mecanismo novo nasce desligado em `ORCH_BILLING_ENABLED=false` e nao compartilha tabelas nem backfill com o legado. `orch_sessions` e a fonte canonica; eventos idempotentes sao agregados em payload imutavel de ate 200 unidades e publicados pela aplicacao Celery dedicada.

A publicacao usa exchange topic durable, mensagem persistente, `mandatory`, publisher confirm, `message_id/header messageId = snapshot_id` e headers de origem/schema/workspace. Somente o confirm roteado permite `sent`; falha depois do envio e antes do commit causa reentrega at-least-once com o mesmo `snapshot_id`.

Retry e leases sao persistentes e indefinidos. Payload invalido fica `blocked`. Operacao, deploy e rollback: `docs/BILLING_BATCH_RUNBOOK.md`.

`UNKNOWN`: confirm/retorno mandatory, binding, consumer e deduplicacao observados no ambiente alvo.

## Target Core — FileApp

Base configurada por `SYNC_WEBHOOK_BASE_URL`. Caminho tipo 1 ativo:

1. `POST /v2/mailings/upload`;
2. `GET /v2/mailings/mapping-templates`;
3. `PATCH /v2/mailings/{id}`;
4. `GET /v2/mailings/{id}/field-mappings`;
5. `PUT /v2/mailings/{id}/field-mappings`;
6. `POST /v2/mailings/{id}/import`;
7. `GET /v2/mailings/{id}` e `POST /v2/flow/{flow_uuid}/mailings`.

Autenticacao usa bearer preferencial ou API key do workspace. Segredos nao devem ser registrados.

`UNKNOWN`: contrato e efeito atual no destino, inclusive garantia de `persons + orch_sessions`.

## Target Core — Runner v5 / `switch_bot_flow`

O ORCH consulta `GET /v2/flow/{flow_uuid}?compact=true` com bearer e `X-WORKSPACE-UUID`, lê `data.summary.runner_token` e o cacheia por workspace/flow. Cada mensagem de usuario e enviada a `POST /v5/runner/tokens/{runner_token}/whatsapp/session` com o conteúdo JSON Meta original; o token fica somente na URL e não deve aparecer em logs. O segmento `whatsapp` e contratual: ele faz o Runner extrair o `wa_id`, manter a identidade da sessao e despachar pela API WhatsApp generica configurada no Target Core, sem exigir integracao ligada ao flow BOT.

O primeiro `202` precisa devolver `session_id`, persistido como identidade da sessao BOT. Mensagens seguintes usam o mesmo endpoint e precisam manter a mesma identidade. O BOT responde diretamente a Meta; o ORCH atua apenas no sentido ORCH -> BOT.

O encerramento possui dois contratos aceitos. A rota explicita `POST /v1/orch/{workspace_uuid}/{flow_uuid}/switch-bot-flow/callback` recebe `session_id`, `status` e erro opcional. O contrato nativo confirmado do `finish_flow` permanece em `POST /v1/orch/{alias}` e envia `entity`, `session.id`, `variables` e `disposition`; o ORCH consome esse envelope antes do trigger comum somente quando `session.id` coincide com o `target_session_id` do handoff. `success/completed/finished` seguem pelo branch `success`; `error/exception/failed/unsuccess` seguem pelo branch `exception_*`. As rotas nao possuem autenticacao propria nesta primeira entrega; a protecao externa permanece pendencia de rollout.

## Files / Arquivos API

Usada para download, metadados, listagem, move e reupload/quarentena. Credenciais sao lidas de `ARQUIVOS_*` com fallback `SYNC_WS_*`.

Eventos fora de pastas monitoradas e em `processados` sao descartados. A pasta `falha` nao possui guard equivalente confirmado.

## Otima LLM

O componente `intelligent_agent` chama endpoints compativeis com Chat Completions ou Responses. Base/gateway/chave e timeout sao configuraveis. Retry de conectividade/5xx nao foi identificado.

## API call

Cards podem chamar URL HTTP arbitraria, com headers/query/body renderizados, timeout e retry/backoff. A chamada e sincrona dentro da execucao async e ocorre com transacao aberta.

Validacao so e considerada completa quando o POST externo e observado no destino.

## Webhook de `finish_flow`

Quando `finish_flow.parameters.webhook` contem uma URL, o executor envia um unico `POST` no formato `{"session": {..., "contact": {...}}, "cdr": {...}}`. `session` contem os campos persistidos publicos e o `result`; `contact` aparece uma vez e `cdr` e o payload cru do evento Dialer selecionado em `orch_channel_events`. `runtime_variables` e estado interno e nao integra o body.

O envio usa timeout de 5 segundos, inclui `Idempotency-Key` deterministica por sessao/evento e roda fora do event loop, mas ainda durante a transacao do workflow. Nao possui outbox ou task dedicada. Fluxo Dialer sem linha correspondente no ledger nao envia body incompleto: registra dispatch adiado e deixa o evento elegivel para retomada. Em falha HTTP, conserva a copia do CDR e o resultado para diagnostico. Cada CDR distinto pode gerar um POST para a mesma sessao, pois representa uma tentativa Dialer sequencial; apos `2xx`, o proprio evento e marcado `finish_flow_webhook_dispatched`, impedindo apenas seu replay.

## SFTP

`generate_file` usa Paramiko e configuracao do card/runtime. O envio e auditado em `orch_generate_file_dispatch_audit`.

Risco: crash apos upload e antes de commit pode repetir o arquivo.

## Supplier

`POST /v1/orch/{workspace_uuid}/{flow_uuid}/resubmit` e a unica rota com autenticacao propria identificada. Usa pares de client id/secret e `event_id` para idempotencia.
