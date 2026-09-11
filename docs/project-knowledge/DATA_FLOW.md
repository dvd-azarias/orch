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

1. Exige `membership_state=active|inactive`, renderiza `person_uuid` no runtime e resolve a lista pelo UUID público do mailing. UUID/estado inválido segue por `exception`; pessoa ou lista ausente segue por `not_found`.
2. Em `active`, a lista precisa estar `READY_TO_INGEST` ou `PROCESSED`. O card cria ou reutiliza o draft da pessoa na `source_list` e, quando existe vínculo ativo da lista com o flow atual, reativa os `contact_list_members` já materializados para essa pessoa.
3. Em `inactive`, a situação da `source_list` não impede a retirada. O card exige vínculo ativo mailing→flow e membros materializados da pessoa nesse par exato de `mailing_id + contact_list_id`; ausência segue por `not_found` sem atingir outro flow.
4. Inativar grava `status=0`, preenche `unassigned_at`, limpa roteamento/tentativas e encerra somente sessões irmãs ainda ativas com o mesmo flow, lista, mailing e pessoa. A sessão que executa o card é excluída dessa parada para concluir a branch configurada.
5. Repetir o mesmo estado é idempotente e segue por `unchanged`; qualquer vínculo-fonte criado, membro alterado ou sessão irmã parada segue por `changed`.
6. A saída em `variables.customs[output_var]` e `source_list_membership_last_result` informa estado anterior/desejado, escopo, contagens de membros alterados e sessões paradas. `sessions_created` é sempre zero.
7. `active` não materializa membro ausente, não recria sessão anteriormente encerrada e não associa mailing ao flow. `inactive` não apaga `source_list_contact_drafts`, `contact_drafts`, membros ou histórico.

## Card `wait_for_event`

1. Ao alcançar o card pela primeira vez, normaliza o envelope, registra o índice atual de `callbacks_pending` e persiste em `workflow_v2.wait_for_event` o card, origem, resultado, instante de bloqueio e prazo imutável.
2. Mantém o cursor no próprio card, grava `frozen_until=timeout_at`, marca `blocked_wait_for_event` e retorna a sessão para `state=0`. O dispatcher não a reivindica antes do prazo.
3. O callback entra pela rota canônica com `event_name=callback`, a mesma `entity` e o `result` esperado. A persistência serializa com o lock `92021/session_id`, anexa o evento e, somente no match exato, remove o congelamento e mantém a sessão elegível.
4. O índice-base impede que callbacks já pendentes antes do armamento liberem o card. Eventos novos com outra origem/resultado e callbacks recebidos depois do prazo são preservados, não consumidos.
5. Na retomada, um callback correspondente recebido até o prazo vence e segue por `received`; sem ele, o instante `timeout_at` segue por `timeout`. O prazo nunca é renovado por reentrada ou callback alheio.
6. O resultado é gravado em `variables.customs[output_var]` e em `wait_for_event_last_result`. O caminho recebido contém `status`, `event_source`, `event_result`, `received_at` e `data`; timeout contém os três primeiros campos e `timeout_at`.
7. Falha de configuração/estado usa `exception` quando conectada; sem essa branch, termina de forma diagnosticável. O card não cria endpoint, fila, ledger, migration ou retry externo próprio.

A correlação continua sendo o contrato Alpha preexistente `flow_uuid + entity`, que seleciona uma sessão ativa. Ela não distingue duas sessões simultâneas do mesmo flow e entidade; consulte R32.

### Composição com `send_with_dialer_handoff`

O novo card de discador usa a espera genérica sem transformar a tabulação em responsabilidade do card:

```text
send_with_dialer_handoff marca linked_actuator=dialer e bloqueia
  -> Dialer informa answered
  -> ORCH segue pela saída answered
  -> wait_for_event(event_name=callback, event_result=tabulation)
  -> callback grava data.outcome=positive|neutral|negative
  -> condition escolhe o braço da jornada
```

O valor público desta composição é `tabulation`, em inglês. `tabulacao` continua reservado ao caminho legado de callback de `run_flow` e não deve ser reutilizado aqui. Para cobrir a corrida em que a tabulação chega depois do início do acionamento, mas antes de o `wait_for_event` ser armado, o novo Dialer fornece ao card um `not_before` igual ao instante de preparação. O card pode então considerar somente callbacks recebidos a partir daquele instante, sem consumir eventos anteriores da sessão. O guard é exclusivo dessa transição; as demais esperas preservam o índice-base histórico.

## Card `send_with_dialer_handoff`

1. É um componente novo e aditivo. `send_with_dialer` continua com o mesmo identificador, configuração, marcador, bloqueio e callbacks.
2. `answer_action=bot` exige o flow BOT próprio do card. `answer_action=human` exige equipe e canal de atendimento, com `queue_voice_uuid` resolvido pelo Target Core no save.
3. O ORCH valida novamente a configuração no runtime e, em uma única escrita sobre o membro exato, marca `linked_actuator=dialer` e materializa `list_validity`. `indefinite` grava `NULL`; `link_date` grava D0; `days_after_link` grava D+N, sempre a partir do `flow_mailing_links.linked_at` ativo convertido para `America/Sao_Paulo`.
4. Em `session_mode=person`, o card exige `select_contact_channel(voice)` anterior. Em `channel`, preserva o membro/endereço que originou a sessão.
5. Configuração ausente preserva a vigência indefinida para definitions anteriores. Modalidade limitada sem vínculo ativo falha de forma terminal antes de deixar atuador ou data parcialmente gravados. Datas PostgreSQL retornadas ao runtime são serializadas em ISO.
6. `send_with_dialer_handoff_routing` registra destino, política normalizada, assignment e instantes. O retorno Dialer usa as mesmas branches normalizadas (`answered`, `busy`, `rejected`, `invalid_number`, `no_answer` e `failed`); o destino pós-atendimento não cria uma segunda engine de tabulação.
7. O Target Core entrega ao consumidor de voz a configuração BOT ou humana. A engine ORCH não chama Runner, Live ou PBX a partir deste card; ela continua sendo a autoridade do marcador e da jornada.

O modo BOT pode reutilizar o consumo já existente de `flow_uuid`, campanha e `runner_token`. O modo humano exige que o consumidor externo reconheça `answer_action.type=human`, use `queue_voice_uuid` e não exija token Runner. Enquanto essa adaptação e um canário PBX não existirem, o envelope humano é contrato preparado, não entrega homologada.

## Card `split_random`

1. Normaliza os percentuais inteiros de `variant_a` e `variant_b`, aceita os extremos `0/100` e `100/0` e exige soma exatamente igual a 100.
2. Antes da seleção, valida no grafo exatamente uma saída `variant_a`, uma `variant_b` e no máximo uma `exception`. Isso impede que o fallback legado do resolvedor de branches encaminhe silenciosamente uma sessão por uma saída diferente da escolhida.
3. Calcula um bucket de `0` a `99` com SHA-256 sobre a identidade estável `flow + session + revision + card`. Bucket menor que o percentual A segue por `variant_a`; os demais seguem por `variant_b`.
4. A mesma sessão, revisão e card produzem sempre o mesmo bucket, inclusive em retry ou redelivery. Sessões distintas são amostradas de forma pseudoaleatória; o percentual é uma probabilidade por sessão, não uma cota exata em lotes pequenos.
5. Grava somente o nome da variante em `variables.customs[output_var]`. O diagnóstico `split_random_last_result` inclui bucket, percentuais, revisão e estratégia, sem persistir o material usado como seed.
6. Configuração ou grafo inválido segue por `exception` quando há exatamente uma saída desse tipo. Sem ela, a sessão é terminalizada uma vez, impedindo repetição permanente pelo dispatcher.

O card não usa gerador aleatório de processo, não acessa rede ou tabelas adicionais, não cria fila, migration ou retry próprio.

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

## Handoff SMS marker-only e fronteira do envio real

Primeira entrega:

```text
select_contact_channel(sms) seleciona um canal telefônico elegível
  -> preserva o tipo de origem (sms/phone/voice)
  -> send_with_sms valida o membro e o endereço exatos
  -> ORCH grava linked_actuator=sms
  -> ORCH bloqueia a sessão
  -> nenhum POST de SMS é executado
```

O M2 implementa essa primeira entrega com um `UPDATE` protegido pela sessão ativa e pelo membro exato. Flow, sessão, identificador, endereço, lista, mailing, pessoa quando disponível e um tipo telefônico elegível (`sms`, `phone` ou `voice`) precisam coincidir. Em escopo `person`, o guard geral exige antes uma seleção ativa de `select_contact_channel`. A compatibilidade é uma regra de capacidade: ela não muda `contact_channel_type`, não escolhe e-mail/WhatsApp e não grava atuador durante a seleção. O marcador, o cursor bloqueado e a transição da sessão para `state=1` participam da mesma transação do dispatcher. Reentrada encontra `blocked_send_with_sms` e não repete o handoff.

O `linked_actuator` informa qual atuador deve assumir o contato, mas não é um payload de dispatch. Diferentemente do caminho WhatsApp com `outbound_hsm`, a primeira entrega de SMS não lê nem materializa mensagem, configuração do provedor, callbacks, idempotência ou credencial segura. O runtime diagnóstico contém somente card, membro, resultado do marcador e instante; token, mensagem, callback e endereço não são copiados.

Ativação futura obrigatória:

```text
ORCH executa a revisão fixada e renderiza o card
  -> materializa outbound_sms ou contrato equivalente
     (sessão + card + membro + revisão + payload + idempotência + segredo protegido)
  -> grava linked_actuator=sms de forma consistente com o envelope
  -> Supplier/emissor reivindica somente envelope completo
  -> emissor realiza o POST ao provedor
  -> adaptador correlaciona DLR/MO/status
  -> ORCH retoma a mesma sessão pela branch correspondente
```

Supplier/Target não deve carregar a definição corrente do flow para reconstruir o SMS. O conteúdo final é responsabilidade da execução do ORCH e deve permanecer ligado à revisão fixada da sessão. Enquanto esse contrato e os callbacks não existirem e não forem comprovados E2E, envio real permanece fora do escopo do card.

## Handoff RCS marker-only e fronteira do envio real

```text
select_contact_channel(rcs) seleciona somente um membro explicitamente RCS
  -> send_with_rcs valida membro, endereço, lista, mailing e pessoa
  -> ORCH grava linked_actuator=rcs
  -> ORCH bloqueia a sessão em state=1
  -> nenhum POST RCS é executado
```

RCS não é inferido de um número telefônico genérico. A Identidade materializa um canal `rcs` separado somente quando recebe `has_rcs=true` em telefone fora de “não perturbe”; o seletor usa correspondência exata e o repositório recusa `voice`, `phone`, `sms`, `whatsapp` e `email`. Em `person`, o card exige seleção explícita anterior; em `channel`, preserva o membro/endereço de origem. Marcador, cursor bloqueado e `state=1` pertencem à mesma transação, e a reentrada em `blocked_send_with_rcs` não repete o handoff.

O runtime armazena apenas o card, o membro e o resultado `marked|already_marked`; a mensagem configurada não é materializada nem copiada. Antes do envio real, uma entrega separada deverá definir a API do provedor, credenciais protegidas, payload final ligado à revisão fixada, idempotência, claim/ACK/retry e callbacks inequívocos. `linked_actuator=rcs` isolado nunca autoriza dispatch.

## Handoff de e-mail marker-only e fronteira do envio real

```text
select_contact_channel(email) seleciona somente um membro explicitamente e-mail
  -> send_with_email valida membro, endereço, lista, mailing e pessoa
  -> ORCH grava linked_actuator=email
  -> ORCH bloqueia a sessão em state=1
  -> nenhum POST ou envio SMTP é executado
```

Em `person`, o card exige seleção explícita anterior; em `channel`, preserva o membro e o endereço de origem. O update protegido exige a mesma sessão ativa, flow, identificador, endereço, lista, mailing, pessoa quando disponível e tipo `email`. Marcador, cursor bloqueado e `state=1` pertencem à mesma transação; a reentrada em `blocked_send_with_email` não repete o handoff.

O runtime guarda somente o card, o membro e o resultado `marked|already_marked`. Remetente, reply-to, assunto e corpos configurados não são copiados. As saídas `sent`, `delivered`, `opened`, `clicked`, `deferred`, `bounced`, `complained`, `unsubscribed`, `failed`, `timeout` e `exception` são contrato visual futuro. Antes do envio real, uma entrega separada deverá materializar envelope ligado à sessão, card, membro e revisão, com conteúdo final, credencial protegida, idempotência, claim/ACK/retry e callbacks normalizados.

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
  -> select_contact_channel valida o membro atual em channel
     ou escolhe explicitamente um membro da mesma pessoa/lista/mailing em person
  -> define linked_actuator apenas quando um card autorizado o exige
```

`session_scope=person` ativa obrigatoriamente o roteamento contextual daquela sessão e exige ao menos um seletor de membro, lista ou mailing. Antes de uma seleção explícita, alcançar `send_with_dialer`, `send_with_sms`, `send_with_rcs`, `send_with_email`, `send_with_whatsapp`, `send_whatsapp_interactive` ou `send_whatsapp_template` terminaliza com `person_scope_channel_component_not_supported`; não há escolha implícita. Após `select_contact_channel` retornar `selected`, o M2 hidrata e reutiliza o membro escolhido nas retomadas, e o card de comunicação permanece responsável por definir seu `linked_actuator`. Ausência de `session_scope` equivale a `channel` e preserva compatibilidade.

Na criação explícita pelo endpoint `/sessions`, um payload com `session_scope=channel` e `contact_list_member_id` válido acrescenta o membro à identidade de reuso. O advisory lock histórico e o `entity_session_id=entity_address:::flow_uuid` permanecem inalterados: o mesmo membro reutiliza sua sessão ativa, mas dois membros diferentes podem gerar duas sessões mesmo quando compartilham pessoa, tipo e endereço. A identidade fica imutável em `runtime_variables.session_identity`; `input_payload`/`last_payload` são fallback para sessões anteriores ao patch. Chamadas `person`, sem escopo explícito ou sem membro válido continuam sob a correlação legada. Isso não resolve a ambiguidade de callbacks que chegam sem uma chave de sessão; consulte R32 e R35.

Em `channel`, `select_contact_channel` só pode selecionar o membro/endereço que já originou a sessão. Em `person`, a busca exige `person_uuid`, permanece dentro da mesma pessoa, `contact_list_id` e `mailing_id`, prioriza o canal marcado como primário e usa o menor `contact_list_member_id` como desempate. O rebind altera apenas `orch_sessions.entity_address` e falha de forma diagnosticável diante de perda de escopo ou colisão com outra sessão ativa. Uma tentativa posterior em `not_found` ou `exception` limpa a seleção anterior e volta a bloquear comunicação até novo `selected`.

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
