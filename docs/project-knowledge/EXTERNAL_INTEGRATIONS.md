# Integracoes Externas

## PostgreSQL / Target data plane

O ORCH usa o mesmo database para schemas `ws_*` e objetos centrais `target.*`. Le dados de workspace e flow e, em componentes especificos, altera tabelas compartilhadas.

Risco: ownership parcial e acoplamento de schema. `linked_actuator_enum` e explicitamente responsabilidade do Target Core.

## RabbitMQ e Redis

RabbitMQ transporta tasks Celery. Redis e usado como backend opcional, heartbeat do beat e locks/cooldowns de reconciliacao.

`UNKNOWN`: policies, DLQ, durabilidade, bindings, consumidores e backlogs reais.

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

O envio usa timeout de 5 segundos, inclui `Idempotency-Key` deterministica por sessao/evento e roda fora do event loop, mas ainda durante a transacao do workflow. Nao possui outbox ou task dedicada. Fluxo Dialer sem linha correspondente no ledger nao envia body incompleto: registra dispatch adiado e deixa o evento elegivel para retomada. Em falha HTTP, conserva a copia do CDR e o resultado para diagnostico. Apos o primeiro `2xx`, novas execucoes nao reenviam, somente o evento utilizado e processado e callbacks tardios nao reabrem a sessao confirmada.

## SFTP

`generate_file` usa Paramiko e configuracao do card/runtime. O envio e auditado em `orch_generate_file_dispatch_audit`.

Risco: crash apos upload e antes de commit pode repetir o arquivo.

## Supplier

`POST /v1/orch/{workspace_uuid}/{flow_uuid}/resubmit` e a unica rota com autenticacao propria identificada. Usa pares de client id/secret e `event_id` para idempotencia.
