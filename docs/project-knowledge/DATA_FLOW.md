# Fluxos de Dados

## Billing batch `service-orch`

```text
orch_sessions INSERT confirmado pela transacao principal
  -> registro idempotente em orch_billing_events (falha aberta via savepoint)
  -> reconciliador repara omissoes a partir de orch_sessions
  -> agregador trava pending com SKIP LOCKED e cria snapshot de ate 200
  -> eventos ficam batched e ligados ao snapshot na mesma transacao
  -> publisher adquire lease/claim_token e envia o payload persistido
  -> confirm + roteamento RabbitMQ: snapshot/eventos sent
  -> falha transitoria: failed + next_attempt_at; payload invalido: blocked
```

Reprocessamento mensal cria eventos ausentes pela mesma chave, reenfileira snapshots existentes sem trocar `snapshot_id` e adia o replay quando o lease esta `processing`. Detalhes em `docs/BILLING_BATCH_RUNBOOK.md`.

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
  -> bootstrap fixa revision_id no runtime/cursor
  -> task advance_session
  -> advisory lock da sessao
  -> carrega exatamente a revisao fixada da sessao
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

O M2, eventos de canal que dependem do grafo e callbacks tardios resolvem a definicao pela
`runtime_variables.workflow_v2.revision_id` da sessao e validam que a revisao pertence ao mesmo flow.
Sessao antiga sem esse campo seleciona a revisao corrente uma vez e grava o pin atomicamente antes de
executar. Pin invalido ou inexistente terminaliza a sessao de forma diagnosticavel; nao ha fallback
silencioso para a revisao corrente.

A garantia forte vale para revisoes publicadas. O fallback historico para draft foi preservado no Alpha;
como o Target Core edita o draft existente, uma sessao fixada em draft ainda pode observar alteracoes sob
o mesmo `revision_id`.

## Card `identidade_person`

1. Renderiza `document` com o runtime e valida CPF de 11 dígitos, UUID do workspace, token e enums do envelope.
2. Consulta a URL fixa da Identidade.io. Uma resposta `2xx` válida é cacheada temporariamente no runtime até o card concluir; token e CPF integral não entram em logs.
3. `data=[]` grava a saída com `found=false` e segue por `nao_encontrado` sem escrita de pessoa/lista.
4. Com pessoa encontrada, normaliza campos e canais. Telefones `do_not_disturb=true` permanecem apenas nos metadados e nunca viram canal acionável; e-mails válidos viram canais.
5. `lookup_only` não escreve. `create_if_missing` preserva pessoa existente. `enrich_if_found` não cria ausente. `upsert` cria ou enriquece. Valores nulos externos nunca apagam dados locais.
6. Quando uma lista é escolhida e existe pessoa local resultante, cria/reutiliza `contact_drafts` e `source_list_contact_drafts` de forma idempotente dentro do mesmo savepoint da pessoa.
7. Quando `link_mailing_to_current_flow` está ativo, a sessão para em `blocked_identidade_person_flow_link` após persistir o runtime e concluir o commit da pessoa/draft. A task dedicada chama `POST /v2/flow/{flow_uuid}/mailings` com `call_origin=identidade_person` e a mesma lista declarada no card.
8. O Target Core valida a autorização na definição executável, materializa ou atualiza `contact_list_members` com `skip_orch_sessions=True` e responde idempotentemente mesmo se o vínculo já estava ativo. A task registra `completed|failed` sem token e reenfileira o executor no mesmo card.
9. `encontrado` só é emitido depois das ações locais e do vínculo solicitado concluírem. Erro lógico, externo ou SQL reverte o savepoint ou segue por `exception` quando mapeado.

A ordem pós-commit é obrigatória: uma chamada síncrona dentro do savepoint não permitiria que o Target Core enxergasse o `contact_draft` recém-criado. Mesmo um vínculo previamente ativo é atualizado pela task para materializar o novo membro, sem criar uma nova sessão ORCH.

## Card `create_contact`

1. Exige ação explícita (`update_current`, `create_if_missing` ou `upsert`), política (`fill_missing` ou `overwrite_non_null`) e mapping com ao menos um valor resolvido.
2. `update_current` resolve a pessoa exclusivamente pelo `person_uuid` do membro contextual já validado para a sessão. O parâmetro `identifier` não participa dessa ação.
3. `create_if_missing` e `upsert` localizam a pessoa pelo `identifier` renderizado. A criação usa `ON CONFLICT DO NOTHING` e relê a pessoa sob lock para suportar concorrência.
4. O mapping aceita somente campos cadastrais de `persons` e caminhos `extra.<campo>`. Valores nulos ou vazios nunca apagam dados; `fill_missing` preserva preenchidos e `overwrite_non_null` substitui somente com valores presentes.
5. Toda escrita ocorre em savepoint e afeta somente `persons`. O card não altera identificador, canais, listas, membros, cursores de outras sessões ou billing.
6. A saída mínima (`action`, `person_uuid`, `identifier`, `changed_fields`) é gravada em `variables.customs[output_var]` e no diagnóstico `create_contact_last_result`.
7. O fluxo segue por `created`, `updated`, `unchanged` ou `not_found`; falhas controladas seguem pela branch `exception` quando conectada.

## Card `source_list_membership`

1. Renderiza `person_uuid` no runtime; aceita a pessoa contextual ou a saída de um card anterior, como `{{contact_action.person_uuid}}`.
2. Resolve a pessoa não mesclada por UUID e a lista pelo UUID público do mailing, ambos sob lock transacional. Template de pessoa não resolvido, pessoa ausente ou lista ausente seguem por `not_found` sem escrita parcial.
3. Aceita somente listas em `READY_TO_INGEST` ou `PROCESSED`. Estado incompatível, UUID inválido ou pessoa sem identificador seguem por `exception` quando a branch estiver conectada.
4. Cria um `contact_draft` com os dados atuais da pessoa ou reutiliza o draft de mesmo identificador já ligado à lista. Canais válidos são copiados/upsertados e os contadores da lista crescem apenas na criação.
5. O lock da linha de `source_lists` serializa inserções concorrentes na mesma lista; repetição retorna `already_linked` e não duplica o vínculo.
6. Atualiza em `persons` somente as referências `last_contact_draft_id`, `last_source_list_id`, `last_mailing_id` e `last_seen_at` relacionadas à associação.
7. A saída é gravada em `variables.customs[output_var]` e em `source_list_membership_last_result`; branches normais são `linked`, `already_linked` e `not_found`.
8. O card não chama o Target Core, não associa a lista ao flow, não materializa `contact_list_members` e não inicia sessões. Esses efeitos exigem comandos separados para evitar recursão/fan-out.

## Card `wait_for_event`

1. Ao alcançar o card pela primeira vez, normaliza o envelope, registra o índice atual de `callbacks_pending` e persiste em `workflow_v2.wait_for_event` o card, origem, resultado, instante de bloqueio e prazo imutável.
2. Mantém o cursor no próprio card, grava `frozen_until=timeout_at`, marca `blocked_wait_for_event` e retorna a sessão para `state=0`. O dispatcher não a reivindica antes do prazo.
3. O callback entra pela rota canônica com `event_name=callback`, a mesma `entity` e o `result` esperado. A persistência serializa com o lock `92021/session_id`, anexa o evento e, somente no match exato, remove o congelamento e mantém a sessão elegível.
4. O índice-base impede que callbacks já pendentes antes do armamento liberem o card. Eventos novos com outra origem/resultado e callbacks recebidos depois do prazo são preservados, não consumidos.
5. Na retomada, um callback correspondente recebido até o prazo vence e segue por `received`; sem ele, o instante `timeout_at` segue por `timeout`. O prazo nunca é renovado por reentrada ou callback alheio.
6. O resultado é gravado em `variables.customs[output_var]` e em `wait_for_event_last_result`. O caminho recebido contém `status`, `event_source`, `event_result`, `received_at` e `data`; timeout contém os três primeiros campos e `timeout_at`.
7. Falha de configuração/estado usa `exception` quando conectada; sem essa branch, termina de forma diagnosticável. O card não cria endpoint, fila, ledger, migration ou retry externo próprio.

A correlação continua sendo o contrato Alpha preexistente `flow_uuid + entity`, que seleciona uma sessão ativa. Ela não distingue duas sessões simultâneas do mesmo flow e entidade; consulte R32.

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

## Preparacao HSM WhatsApp para Supplier

Ao alcançar `send_with_whatsapp`, `send_whatsapp_interactive` ou `send_whatsapp_template`, o M2 seleciona ANI, resolve o template do card em foco, interpola o payload com o contato/runtime e grava `contact_list_members.outbound_hsm`. Roteamento, consumo do limite e HSM ficam no mesmo savepoint; falha reverte o conjunto. Branch `exception*` do card recebe erros lógicos de HSM; sem branch, a sessão termina com código `whatsapp_hsm_*`.

O Target Core é consumidor desse estado: seu Contact Supplier seleciona somente linhas WhatsApp com HSM materializado e devolve o JSON sem carregar ou interpretar a definição do flow.

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

## Escopo de sessão recebido do Target Core

```text
Target Core associa mailing
  -> channel: uma sessão por canal, com member_id + endereço + tipo exatos
  -> person: uma sessão por pessoa, com um membro-semente determinístico
ORCH recebe a sessão
  -> valida membro/lista/mailing + endereço da sessão + tipo informado
  -> executa cards genéricos
  -> define linked_actuator apenas quando um card autorizado o exige
```

`session_scope=person` ativa obrigatoriamente o roteamento contextual daquela sessão e exige ao menos um seletor de membro, lista ou mailing. Se ela alcançar `send_with_dialer`, `send_with_whatsapp`, `send_whatsapp_interactive` ou `send_whatsapp_template`, o M2 terminaliza com `person_scope_channel_component_not_supported`; não escolhe outro canal implicitamente. Ausência de `session_scope` equivale a `channel` e preserva compatibilidade.

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
