# Fluxos de Dados

## Trigger comum

```text
TRIGGER       POST /v1/orch/{workspace_uuid}/{flow_uuid}
ENTRYPOINT    app/api/v1/orch.py::_trigger_orch_for_workspace
VALIDATION    workspace ativo/completed; payload pelo schema FastAPI
DECISION      detect_app: Arquivos -> WhatsApp -> Dialer -> Generic
PROCESSING    app/services/orch_trigger_service.py::process_single_payload
PERSISTENCE   orch_sessions; opcionalmente channel_events/discarded/alarms
ASYNC         advance_session task quando Celery ativo
OUTPUT        202 OrchTriggerAccepted
ERROR         erros HTTP padronizados; alarmes best-effort
```

`GenericApp` aceita qualquer dicionario nao vazio nao classificado antes. `ArquivosApp` exige `payload.file` como dicionario; sinais S3 isolados nao bastam.

## Ciclo de sessao

1. Extrair `entity`, `entity_type`, `entity_address`, `entity_session_id`.
2. Obter advisory lock pela chave logica.
3. Reusar sessao elegivel ou criar uma nova.
4. Preservar `entity_origin_app`; atualizar snapshots do evento corrente no runtime.
5. Registrar eventos de canal no ledger quando aplicavel.
6. Bootstrap do workflow somente quando ainda nao inicializado.
7. Executar ou enfileirar M2.

Regra normal de reuso: mesmo flow/entidade/tipo/endereco, `state <> 3` e `unassigned_at IS NULL`.

Excecoes: WhatsApp e Dialer possuem caminhos de correlacao por endereco/session id e podem retomar sessoes finalizadas. Callbacks, tabulacao e hangup podem usar janela temporal; sem correlacao, sao descartados e auditados, nao criam sessao arbitrariamente.

## Workflow M1/M2

```text
flow_v2 + flow_v2_revision
  -> maior publicada ou maior draft
  -> bootstrap runtime/cursor
  -> task advance_session
  -> advisory lock da sessao
  -> loop de cards
     -> persiste cursor/runtime
     -> finish | wait | block | error | max steps
  -> estado final/resumivel/bloqueado
```

Paradas relevantes:

- `finish_flow`: persiste o estado terminal; quando `parameters.webhook` esta configurado, envia `session` com campos persistidos, `result` e contato normalizado, mais `cdr` com o payload cru do evento Dialer selecionado no ledger. `runtime_variables` nao sai. Em fluxo Dialer, a ausencia de CDR adia o POST em vez de enviar `null`; cada CDR distinto da mesma sessao pode disparar um POST, e o `2xx` marca somente aquele evento como entregue. Falha mantem CDR/evento para diagnostico e retomada.
- `wait/scheduling_moment`: `frozen_until`, retorna a pending quando elegivel.
- cards de canal e `run_flow`: mantem contexto bloqueante.
- componente desconhecido: caminho Celery pode classificar como fatal e finalizar.
- `session_execution_locked`: outra execucao detem o lock.

Risco: M2 relê a revisao corrente em vez de garantir uso da `revision_id` do bootstrap.

## FileApp — decisao

A resolucao considera UUID de template no evento e configuracao do flow. Template ausente ou nao resolvido conduz ao caminho `tipo_2`; um valor presente mas invalido nao produz erro obrigatorio.

### Tipo 1

```text
FileApp em pasta monitorada + mapping template resolvido
  -> API enfileira ingest_tipo1_event
  -> ingest enfileira process_tipo1_event
  -> download do arquivo pela Files API
  -> Target Core upload
  -> lista/aplica mapping template
  -> GET/PUT field mappings ate READY_TO_INGEST
  -> POST import
  -> enqueue associate_mailing com countdown
  -> move/reupload para processados; em falha, quarentena
  -> task consulta estado do mailing
  -> POST /v2/flow/{flow_uuid}/mailings
```

O ORCH nao persiste diretamente `persons` ou `orch_sessions` neste caminho atual. O efeito final obrigatorio depende do Target Core e permanece `UNKNOWN` sem E2E + SQL.

Retries observados: ingest na falha de publicar proxima task; processo tipo 1 seletivo; associacao ate oito retries; download/move com tentativas proprias.

Divergencias canonicas conhecidas:

- nao ha campo de log literal `decision=fileapp_tipo1`;
- `detach_all_files` pode preencher `mailing_ids_removed`;
- a regra local de uma `source_list` por `file.id` nao esta imposta no caminho ativo;
- Celery/FileApp desabilitado muda o caminho para processamento local, mesmo quando ha template.

### Tipo 2

```text
FileApp sem template resolvido
  -> persiste sessao do evento na API
  -> enqueue ingest_event
  -> enqueue process_event
  -> download CSV
  -> parse linhas
  -> para cada linha: process_single_payload
  -> sessoes/runtime/workflow por linha
```

Falhas por linha podem ser contabilizadas sem falhar a task inteira; nao ha retry geral do processamento tipo 2.

## Eventos de canal

WhatsApp e Dialer sao extraidos para `orch_channel_events`. O ledger suporta claim FIFO e dedupe por sessao/canal/evento/tipo. Um reconciliador procura sessoes com eventos pendentes stale e reenfileira execucao.

Existe tambem um guard anterior baseado nos timestamps da sessao; ele pode descartar status repetido antes do ledger. Impacto real permanece `LIKELY`.

## `switch_bot_flow` — hub WhatsApp para BOT

```text
Meta -> POST canonico do ORCH -> orch_sessions + orch_channel_events
     -> M2 alcanca switch_bot_flow e bloqueia no proprio card
     -> worker dedicado resolve/cacheia runner_token do flow alvo
     -> POST /v5/runner/tokens/{runner_token}/whatsapp/session
        body = mesmo conteudo JSON Meta recebido pelo ORCH
     -> persiste target_session_id e permanece ativo
     -> novos payloads Meta de usuario repetem o relay pelo mesmo endpoint
     -> status sent/delivered/read/failed sao consumidos localmente, sem relay
     -> finish_flow BOT faz POST no alias curto do flow ORCH
        body = entity + session.id + variables + disposition
     -> ORCH correlaciona session.id == target_session_id antes do trigger comum
     -> success ou exception_* -> M2 continua, sem criar nova sessao
```

O primeiro POST nao usa payload sintetico: ele repassa o `runtime_variables.last_payload` que levou a sessao ate o card. O `runner_token` e fixado por flow via cache em memoria/Redis e relido apenas quando ausente ou rejeitado com `401/403`. O `run_flow` permanece independente e inalterado.

O canario de 2026-08-27 confirmou o callback nativo do `finish_flow` no alias curto ja configurado (`POST /v1/orch/{alias}`). O envelope observado usa `session.id` como sessao Runner e `disposition.category/code` como resultado terminal. A interceptacao e habilitada apenas no caminho por alias e so consome o evento quando encontra handoff do mesmo flow com `target_session_id` exato; sem correlacao, preserva o trigger legado.

O contrato e de entrega ao menos uma vez: timeout ou crash entre aceite externo e commit local pode repetir o mesmo payload. A deduplicacao efetiva pelo `messages[].id` no Runner ainda requer comprovacao E2E.

## Generate file

```text
card generate_file
  -> resolve mapping/destino
  -> upsert job + buffer row
  -> beat scan_due
  -> run task
  -> claim SKIP LOCKED
  -> serializa/agrega
  -> SFTP
  -> marca rows/auditoria/runtime
```

O upload SFTP ocorre antes do commit que registra o sucesso; crash nessa janela pode repetir efeito externo.

## Resubmit Supplier

Endpoint autenticado por par de headers. Usa `event_id` como chave de replay, normaliza endereco, unassign de sessoes anteriores e cria novo ciclo pelo workflow padrao.
