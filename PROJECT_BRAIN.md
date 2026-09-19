# ORCH — Project Brain

Baseline inicial de sustentacao criada em 2026-08-24 conforme `PROJECT_STEWARD.md`.

Esta memoria descreve o comportamento confirmado no repositorio. Ela nao comprova, por si so, o estado atualmente implantado em producao. Use os marcadores:

- `CONFIRMED`: comprovado por codigo executado, testes, configuracao versionada ou historico Git.
- `LIKELY`: evidencias fortes, ainda sem confirmacao de runtime.
- `UNKNOWN`: depende de ambiente, dados ou sistemas externos nao observados.

## READ THIS BEFORE CHANGING ANYTHING

1. Este repositorio e um Alpha em producao. A regra e `STABILITY OVER ELEGANCE` e a mudanca padrao e `MINIMUM SAFE CHANGE`.
2. A rota canonica e `POST /v1/orch/{workspace_uuid}/{flow_uuid}`. O `workspace_uuid` seleciona o schema `ws_<uuid>` e deve estar ativo/completo.
3. Nao misture stack manual e `launchd`. Em DEV, use `scripts/dev_phase_stack.sh`. Em producao, o host canonico e `10.1.20.237`, com runtime em `/etc/gohp/orch` e 21 units systemd escaladas; acesso, credencial e inventario ficam em `PROJECT_STEWARD.md`. Os templates genericos de `systemctl/` nao representam literalmente essa instalacao.
4. Filas sao contrato operacional. Use `ORCH_QUEUE_PROFILE` e filas isoladas; nunca reutilize filas de outras aplicacoes sem ordem explicita.
5. FileApp decide `tipo_1` ou `tipo_2` pela resolucao de `mapping_template`. `tipo_1` delega a importacao ao Target Core; `tipo_2` persiste sessoes no ORCH. O efeito final `persons + orch_sessions` do `tipo_1` ainda exige comprovacao E2E externa.
6. O codigo atual nao implementa autenticacao para trigger, consultas ou endpoints admin de migration. Protecao externa e `UNKNOWN`.
7. O risco de amplificacao deixou de ser apenas estatico: em 2026-08-24, sessoes invalidas de um flow draft acumularam 1.154.025 falhas. As sessoes `256`, `257` e `263` foram terminalizadas de forma auditada; consulte `docs/project-knowledge/INCIDENT_HISTORY.md` antes de intervir em dispatcher, reconciliador, filas ou sessoes.
8. Bloqueios considerados sucesso tambem podem ser amplificados sem alarme. A auditoria posterior a migracao dos workers para o host `10.1.20.237` confirmou o loop `blocked_send_whatsapp_interactive` ativo em tres workspaces, mais de 213 mil execucoes de executor e 428 mil metricas em cerca de 70 minutos. A correcao Alpha inclui esse motivo em `BLOCKING_RUNNING_STOP_REASONS`, preservando a sessao em `state=1` ate callback/reconciliacao; implantacao e validacao de runtime ainda estao pendentes.
9. Em 2026-09-06, o `origin/main` coletou 485 testes: 459 passaram e 26 falharam; as falhas correspondem a casos legados que ainda chamam `trigger_orch(flow_uuid=...)` e nao fazem parte da fixacao de revisao. O branch `fix/pin-session-flow-revision` coletou 493: 467 passaram e as mesmas 26 falharam. Nao trate a suite completa como verde, mas tambem nao atribua essa baseline ao patch.
10. Nao conclua runtime apenas por leitura ou teste unitario. Fluxos com DB, broker, API externa ou SFTP exigem evidencia fora da sandbox.
11. Billing possui dois mecanismos mutuamente exclusivos e desligados por default. `ORCH_BILLING_SNAPSHOT_ENABLED` e legado; `ORCH_BILLING_ENABLED` ativa o batch novo somente apos migration `0022`, worker e Beat dedicados. Nunca reutilizar o backfill legado. Consultar `docs/BILLING_BATCH_RUNBOOK.md`.
12. A PR `#142`, implantada no commit de merge `b614a73`, faz a sessao executar a `revision_id` gravada no bootstrap, inclusive em retomadas dependentes do grafo. A stack local, dois smokes e um canario de producao N -> N+1 confirmaram runtime/metricas com o mesmo pin: a sessao pausada `7285` permaneceu em N e a sessao nova `7286` iniciou em N+1. Draft continua mutavel e nao recebe a mesma garantia forte de uma revisao publicada.
13. Valores PostgreSQL `DATE` lidos para o runtime precisam ser convertidos para ISO antes de persistir `runtime_variables`. Em 2026-09-06, `contact_birth_date` cru causou 661 retries da sessao `7324`; a correcao minima em `_inject_contact_runtime_scope` foi implantada nos hosts `10.1.20.136` e `10.1.20.237`. O canario pos-deploy `7341` terminou em `state=3`, sem alarmes, com data ISO, escrita controlada e POST externo confirmado; os dados temporarios foram restaurados.
14. `source_list_membership`, exibido como **Gerenciar Contato na Lista**, exige estado explícito e pertence a flows `person`. `mailing_source=session_origin` resolve a lista pelo membro, flow e vínculo ativo que originaram a própria sessão; `selected` preserva a seleção explícita anterior. Definições antigas sem esses campos continuam equivalentes a `selected + organization_only`. Em `membership_purpose=organization_only`, o contrato anterior permanece: `active` cria/reutiliza o draft, `inactive` atua somente nos membros já materializados do flow atual e nenhuma ação associa mailing ao flow ou cria sessão. Em `current_flow_operational`, permitido apenas para lista específica e estado ativo, o card bloqueia e commita antes de pedir ao Target Core a associação protegida sem fan-out, retoma no mesmo card/sessão, confirma a materialização e grava um escopo pessoa/lista que somente `select_contact_channel` pode consumir. O card de lista não escolhe canal e não escreve `linked_actuator`.
15. `wait_for_event` reutiliza exclusivamente o callback generico existente. A engine mantém a sessão em `state=0` com `frozen_until`, usa baseline de `callbacks_pending` contra evento antigo e serializa callback/executor pelo lock da sessão. O merge `ce47641` foi implantado nos hosts `10.1.20.136` e `10.1.20.237`; os canários `7398`/`7399` confirmaram `timeout`/`received`, oito métricas por sessão, zero alarmes e ausência de hot loop.
16. `api_call_missing_url` pode depender de dado runtime e, portanto, não é totalmente evitável pela validação estática do Target Core. Em 2026-09-06, as sessões de produção `809`/`810` do flow `2112aa34-0c48-4cd6-a477-d8b5f5e1f52e` resolveram `{{contact.extra.callback_url}}` para vazio e foram reenfileiradas pelo reconciliador devido a eventos de canal pendentes, acumulando mais de 53 mil alarmes na fotografia investigada. A correção Alpha preparada usa o branch `exception` quando presente, terminaliza uma vez quando ausente e exclui sessões terminadas da seleção do reconciliador antes do `LIMIT`; está validada localmente, mas ainda não implantada.
17. O card `select_contact_channel` da PR ORCH `#154` foi implantado em `10.1.20.136` e `10.1.20.237`. Depois do deploy, o flow `c114383d-72e1-4401-8877-765e5bfac27f` entrou na allowlist `person` do Target Core nos quatro hosts. O vínculo oficial de um mailing com duas pessoas gerou exatamente as sessões `7513`/`7514`: ambas selecionaram o canal `voice` primário, terminaram em `state=3`, sem erro, e produziram os POSTs externos `1351296`/`1351297` confirmados no `api-bin`.
   - Complemento Gate 3B2/3C: `select_contact_channel` preserva `first_eligible` como default e oferece `next_eligible` somente em `session_scope=person`. Para voz após o novo Dialer, `respect_dial_rule` exige `next_phone` terminal da Supplier V2; `flow_override` solicita a troca pelo grafo, mas continua submetido aos limites rígidos da Supplier. O ORCH envia identidade completa, aceita somente o membro autorizado, não altera `linked_actuator` e registra o consumo por evento/seletor/modo para replay idempotente. `blocked_by_policy` é branch de negócio, não retry técnico. Quando a única barreira é `calendar_closed`, a Supplier V2 devolve `deferred` com a próxima abertura; o ORCH preserva canal e autorização, mantém o cursor no seletor e agenda a mesma sessão por `frozen_until`, sem percorrer um branch do canvas. Desativar na lista o membro contextual limpa a seleção no mesmo estado de runtime. Channel, card legado e Supplier V1 permanecem inalterados.
18. `send_with_sms` preserva o handoff marker-only quando o Gate de canal V2 está desligado: a engine grava `linked_actuator=sms` no membro exato e bloqueia a sessão. Sob `CHANNEL_SUPPLIER_V2_ENABLED` e allowlists de workspace/flow, o mesmo commit persiste uma intenção cifrada e idempotente pinada à sessão, revisão, card, lista, membro, canal e sequência; somente depois do commit uma fila exclusiva a registra em `POST /v2/contact-supplier/channel-dispatches`. O ORCH nunca chama o provedor. O Target revalida a revisão executável, o card, o membro e o marcador antes do outbox. Um segundo Gate, `CHANNEL_SUPPLIER_V2_CALLBACKS_ENABLED`, troca os callbacks estáticos por URLs assinadas do ORCH: status/DLR correlacionado libera a sequência linear e MO é preservado como `callback/response` para um `wait_for_event` subsequente. Callback histórico é auditado sem reabrir a sessão. Implementação e testes locais estão concluídos; deploy/canário permanecem pendentes. Consulte R33 e `CHANNEL_DISPATCH_V2_PLAN.md`.
19. `send_with_rcs` mantém elegibilidade estrita: somente membro `contact_channel_type=rcs` pode ser marcado. Com o Gate de canal V2 desligado, continua marker-only; quando habilitado para o canário, materializa o template, suas variáveis e credencial dentro do mesmo envelope cifrado usado pelo SMS e delega o POST real exclusivamente à Supplier V2. O marcador isolado nunca autoriza envio. `status=V` comprova somente aceite do provedor, não entrega/leitura. Com o Gate 2, a mensagem recebe callbacks oficiais do ORCH, `sent` permanece telemetria e a sessão segue por `delivered|read|response|unavailable|failed|expired|timeout` conforme evento de conclusão e branches do card. Identidade assinada, ledger idempotente, commit antes da fila e descarte tardio estão validados localmente; deploy/canário permanecem pendentes. Consulte R34.
20. O endpoint manual do Target Core preserva `entity_session_id=entity_address:::flow_uuid`, mas, quando recebe explicitamente `session_scope=channel`, usa também o `contact_list_member_id` como identidade de reuso. A correção integrada pela PR `#160` impede que membros Voice/WhatsApp/RCS com o mesmo endereço colapsem na mesma sessão, sem migration e sem alterar callbacks ou chamadas sem escopo. Consulte R35.
21. `send_with_email` segue o padrão marker-only de comunicação: exige um membro explicitamente `email`, grava `linked_actuator=email` e bloqueia sem SMTP/HTTP. O catálogo provider-neutral declara remetente, reply-to opcional, assunto, conteúdo `text|html|both`, evento positivo e branches de ciclo de vida; esses conteúdos não são copiados para runtime e as branches ainda não processam callbacks. Envio real exigirá envelope materializado, credencial protegida, idempotência e normalização de eventos em entrega separada.
22. `send_with_dialer_handoff` é uma extensão aditiva do discador para o blueprint integrado. O card legado `send_with_dialer` permanece aceito e com o mesmo runtime. O novo card valida `answer_action=bot|human`, marca exclusivamente `linked_actuator=dialer` e bloqueia; no retorno `answered`, pode compor com `wait_for_event(callback/tabulation)` e uma condição sobre `wait_event.data.outcome`. Sua vigência opcional materializa em `contact_list_members.list_validity` a última data elegível, calculada sobre o `flow_mailing_links.linked_at` ativo em `America/Sao_Paulo`; ausência da configuração preserva `NULL`/indefinida. O destino BOT é exposto pelo Target Core no envelope de voz. O destino humano também expõe time, canal e `queue_voice_uuid`, mas a entrega PBX direta ainda depende de suporte explícito no consumidor externo do envelope e não está homologada E2E.
23. A UI operacional de Gestão de Extensões no `10.1.20.239` reúne Perfis de Discagem, Listas de Restrição e Telecom. O nome técnico `dialing-management-demo` é histórico; a aplicação e seus dados não são descartáveis. Sua fonte oficial é o repositório privado `GOHP-LAB/target-extensions-ui`, baseline `3035362`, com CI Node `22.17.0`. O Node global do host é `18.19.1` e não pode construir o Vinext atual; no servidor, build e runtime chamam diretamente o CLI Vinext com o Node 22 dedicado. O servidor, a pasta histórica e `/private/tmp` não são fonte do código. A inspeção visual canônica usa `@playwright/cli` e `playwright-cli open http://10.1.20.239:8300/`; HTTP Basic deve ser fornecido por configuração temporária fora do Git, nunca pela URL. Consulte `DIALING_MANAGEMENT_UI_RUNBOOK.md`.
24. O registro de ciclo Supplier V2 do `send_with_dialer_handoff` é opt-in por `DIALER_SUPPLIER_V2_ENABLED` e allowlists explícitas de workspaces e flows. Depois de marcar o membro e persistir a intenção idempotente ligada a sessão, revisão, card, lista, membro e Perfil, o ORCH publica uma task pós-commit na fila própria `orch_dialer_supplier_v2`; o Target Core fixa o snapshot publicado e devolve o ciclo `ready`. O ORCH nunca persiste nem registra em log o `callback_token`. Erros transitórios usam retry limitado; o reconciliador consulta somente os workspaces permitidos e recupera intents `pending`, claims `registering` vencidos e `pending_retry` stale preservando a contagem. `422` fica terminal e alarmado, sem liberar a sessão. O retorno da Gate 2D entra por endpoint interno autenticado, exige correspondência integral de evento/ciclo/sessão/flow/revisão/card e só então libera o branch terminal; `release_mapping_version` é opcional para compatibilidade, aceita somente `pdial_v1` quando presente, é persistida e participa da idempotência. Callbacks brutos do PBX não avançam uma sessão registrada no Supplier V2 e são descartados de forma auditável para impedir hot loop. O canário real de 2026-09-15 confirmou um ciclo, uma tentativa e terminal `answered`, mas também revelou corrida entre o replay idempotente do registro e o callback terminal. A invariável corrigida é: `terminal_received`/`terminal_delivery` sempre vence uma task de registro atrasada; o patch JSONB usa compare-and-set, o claim terminal não chama HTTP e uma escrita que perde a corrida não gera retry nem alarme. Replay autenticado pode refletir `ready`, `deferred` ou `terminal`, enquanto criação nova continua exigindo `ready`. O Gate O1 multilane acrescenta uma autorização independente, fail-closed, para que a mesma sessão alcance posteriormente outro card: o ciclo terminal anterior é preservado em `workflow_v2.dialer_supplier_v2_history`, o novo card recebe uma intenção independente e callback histórico jamais reenfileira o card ativo. O marcador e a intenção são protegidos pelo mesmo savepoint. Em 2026-09-17, o canário também provou uma corrida curta entre o avanço disparado pelo callback bruto e a retomada terminal: o advisory lock fazia a task terminal encerrar como sucesso sem avançar o branch. Somente o endpoint terminal Supplier V2 passou a usar uma task dedicada que repete `session_execution_locked` com backoff limitado; o avanço comum continua com a semântica anterior. Card legado, Supplier V1 e flows de card único permanecem fora dessa mudança. Kerberos K1 e `service_dialer` D1 já foram implantados sob fail-closed; o canário de dois cards foi publicado, porém a homologação comportamental ainda depende do contrato Person/Channel/Dial Rule abaixo. Depois dela, o fluxo completo `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152` deverá ser revisto e ajustado ao contrato final antes de ser retomado.
25. `session_mode=channel` e `session_mode=person` são contratos distintos e ambos permanecem suportados. Em `channel`, o membro que originou a sessão é âncora imutável e cards de seleção apenas validam essa âncora; trocar silenciosamente de endereço é proibido. Em `person`, a sessão representa a pessoa e cards seletores passam a ser a única autoridade para escolher ou avançar o membro, antes dos cards consumidores de canal. O seletor não grava `linked_actuator`; essa escrita continua exclusiva do card consumidor. Dial Rule/Supplier decide elegibilidade, limites e próxima ação; ORCH decide grafo, sessão e seleção. `flow_override` pode alterar somente preferências flexíveis e nunca ultrapassa limite duro, lista de restrição, validade, consentimento, janela legal ou bloqueio terminal. A auditoria read-only de 2026-09-16 classificou 142 orquestrações em 60 workspaces: `126 legacy_channel + 14 channel + 2 person`; seis incompatibilidades foram encontradas, todas em flows publicados do workspace DEV Highcomm, sem alteração de dados. O contrato base está implementado e exercitado; extensões e evidências ficam consolidadas em `FLOW_SESSION_SCOPE_CONTRACT.md`. O canário multidialer e o flow completo só devem ser retomados depois da homologação isolada dos cards de contato.
26. O Rastreamento de Jornadas é uma superfície somente leitura para suporte, exposta pelo ORCH sob `/v1/orch/{workspace_uuid}/observability` e consumida exclusivamente pelo BFF da Gestão de Extensões. A visão agregada usa métricas de card no período e mantém a revisão executada; a visão individual resolve a pessoa por UUID e pelo vínculo histórico de `contact_list_member_id`, cobrindo `person` e `channel` sem correlacionar globalmente telefones iguais. O canvas retornado é estrutural e não expõe parâmetros, tokens, runtime ou callbacks; identifiers e endereços são mascarados. Credencial dedicada, período, paginação, limite de trace e `statement_timeout` são fail-closed/limitados. A entrega não altera runtime nem exige migration. Depois da homologação, o objetivo volta ao flow completo `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`. Consulte `JOURNEY_TRACKING.md`.
27. O canário `identidade_person` de 2026-09-18 confirmou `found`, `not_found`, criação, enriquecimento e repetição sem duplicar a pessoa local. Também revelou amplificação: sem branch `exception`, `identidade_person_invalid_document` escapava da transação e a sessão permanecia disponível ao dispatcher; a sessão canária `8236` chegou a 735 execuções antes do `unassign` oficial. O patch Alpha preparado considera todo erro `identidade_person_*` terminal após as tentativas internas quando não há branch, preservando o roteamento explícito para `exception` quando existe. A correção está validada localmente e ainda não foi implantada.
28. Uma revisão pinada pode ser histórica e anterior às validações atuais. Se
    seu cursor apontar para card ausente, o ORCH deve terminalizar uma única
    vez com `workflow_v2.terminal_failure.code=component_not_found`, limpar o
    cursor seguinte e alarmar; nunca tratar esse caso como fim normal do flow.
29. O bootstrap de contatos em sessão `person` sem membro operacional permite
    somente `create_contact`, `identidade_person` e encerramento explícito até
    existir uma pessoa local canônica. A adoção grava
    `workflow_v2.person_adoption` e atualiza o escopo de contato sem fabricar
    membro, endereço ou `linked_actuator`; `lookup_only`/`not_found` não adotam.
    Depois da adoção, `source_list_membership` é permitido, mas seletor e
    consumidor continuam fail-closed até existir membro contextual real.
    UUID/identificador divergente termina com
    `contact_person_identity_conflict`. Os canários A/F1/B (`8341`–`8345`)
    provaram criação, repetição idempotente, vínculo sem sessão filha e
    enriquecimento de pessoa existente. A mudança está validada localmente e
    ainda não foi implantada.
30. Em sessão `person`, `external_id` identifica a execução e não a pessoa. O
    `select_contact_channel` precisa resolver e reancorar o membro por
    `person_uuid + contact_list_id + mailing_id`; exigir que
    `orch_sessions.entity` seja igual a `contact_list_members.contact_identifier`
    rejeita webhooks válidos cuja correlação da sessão difere do identificador
    do cliente. Essa igualdade continua obrigatória em `channel`, junto do
    membro e endereço de origem, para preservar a âncora imutável. O canário G
    `716c84c9-f0c5-4d07-83ab-f983445d6c97` comprovou o gap na sessão `8411`;
    a correção está validada em testes locais e aguarda rollout canário.

## O que e o ORCH

O ORCH recebe eventos externos heterogeneos, identifica sua origem, correlaciona ou cria sessoes por workspace, carrega uma definicao `flow_v2`, executa cards do workflow e coordena efeitos assincronos por Celery.

Aplicacoes detectadas, nesta ordem: `ArquivosApp`, `WhatsApp`, `DialerApp`, `GenericApp`.

## Runtime confirmado no repositorio

| Processo | Papel | Filas logicas |
|---|---|---|
| API FastAPI/Uvicorn | health, triggers, consultas, admin e enqueue | publica `execute` e FileApp ingest |
| Worker workflow | dispatch, execucao, relay BOT, registro Supplier V2 e heartbeat | `dispatch`, `execute`, `switch_bot_flow`, `dialer_supplier_v2`, `channel_supplier_v2`, `heartbeat` |
| Beat workflow | dispatch, heartbeat e reconciliacoes | publica nas filas acima e FileApp process |
| Worker FileApp | ingest, process, associacao e reconciliacao | `fileapp_ingest`, `fileapp_process`, `fileapp_mailing_assoc` |
| Worker generate_file | scan e envio SFTP | `generate_file_scan`, `generate_file_run` |
| Beat generate_file | scan periodico | publica `generate_file_scan` |
| Worker billing | agrega, publica, reconcilia e reprocessa | `orch.billing.outbox` |
| Beat billing | quatro schedules exclusivos | publica em `orch.billing.outbox` |

Entrypoints:

- API: `app.main:app`.
- Celery: `app.core.celery_app:celery_app`.
- CLI: `python -m app.cli migrate-all` e `python -m app.cli migrate-workspace <uuid>`.
- DEV: `scripts/dev_phase_stack.sh`.

## Principais fluxos

- Trigger comum: valida workspace -> detecta app -> correlaciona/persiste sessao -> registra eventos de canal -> bootstrap -> enqueue/executa M2 -> `202`.
- Workflow: seleciona revisao no bootstrap -> fixa `revision_id` no runtime -> executa essa revisao sob lock da sessao -> persiste cursores -> finaliza, pausa ou bloqueia.
- FileApp `tipo_1`: valida pasta/template -> Celery ingest/process -> Target Core upload/mapping/import -> task de associacao -> pos-processamento do arquivo.
- FileApp `tipo_2`: persiste sessao do arquivo -> baixa/expande CSV -> processa cada linha pelo trigger comum.
- Canal/callback: correlaciona sessao ativa ou recente -> registra ledger/runtime -> retoma card bloqueante; sem correlacao, audita descarte.
- `switch_bot_flow`: bloqueia a sessao ORCH no card, resolve/cacheia o `runner_token` do flow alvo e repassa somente payloads Meta com mensagem de usuario ao Runner v5; callback terminal libera `success` ou `exception_*`.
- `identidade_person`: consulta CPF na Identidade.io e, conforme política explícita, somente retorna, cria ou enriquece `persons`; associação opcional a `source_lists` é transacional e o vínculo ao flow é retomado pós-commit pelo contrato seguro do Target Core.
- `source_list_membership`: aplica `active|inactive` à pessoa/lista e segue por `changed`, `unchanged`, `not_found` ou `exception`. O propósito padrão apenas organiza; o propósito operacional associa/materializa a lista por contrato protegido pós-commit, sem criar sessão filha, e entrega o escopo à seleção explícita de canal.
- `wait_for_event`: arma uma espera finita pelo callback genérico, retoma por `received` ou `timeout` e preserva eventos anteriores ou não correspondentes.
- `send_with_dialer_handoff`: preserva o marcador Dialer e acrescenta destino pós-atendimento BOT ou humano; quando o Gate 3 está habilitado para o workspace/flow, registra de forma assíncrona um ciclo Supplier V2 pinado à revisão e ao Perfil. Com a Gate 2D, somente a decisão terminal correlacionada do Supplier libera o branch do Dialer; o `wait_for_event` continua responsável pela tabulação posterior desenhada no canvas. O card Dialer legado permanece inalterado.
- `send_with_sms` / `send_with_rcs`: marcam o membro exato e bloqueiam a sessão. Sob Gate/allowlists exclusivos, persistem envelope cifrado na mesma transação e o registram pós-commit no outbox da Supplier V2; aceite HTTP do provedor nunca basta. Sob o Gate separado de callbacks, URLs assinadas do ORCH alimentam o ledger idempotente e retomam SMS linearmente ou RCS pela branch configurada/timeout.
- Generate file: card grava job/buffer -> beat scan -> worker produz arquivo SFTP -> auditoria e runtime.

Detalhes: `docs/project-knowledge/DATA_FLOW.md`.

## Persistencia

Objetos ORCH por schema de workspace:

- `orch_sessions`, `orch_sessions_alarms`, `orch_session_metrics`, `orch_discarded_events`, `orch_channel_events`;
- `orch_generate_file_job`, `orch_generate_file_row_buffer`, `orch_generate_file_dispatch_audit`;
- `orch_whatsapp_limits`, `orch_whatsapp_rate_limit_per_flow`;
- legado `orch_billing_usage_snapshots`; batch `orch_billing_events`, `orch_billing_snapshots`, `orch_billing_reprocess_requests`;
- `orch_alembic_version`.

Objeto central criado pelo ORCH: `target.orch_flow_aliases`.

Objetos compartilhados consumidos ou alterados em fluxos especificos: `target.workspaces`, `flow_v2`, `flow_v2_revision`, `source_lists`, `persons`, `contact_list_members`, `cache_card_store`.

Detalhes e ownership: `docs/project-knowledge/DATABASE.md`.

## Integracoes externas

- PostgreSQL/PgBouncer: estado, flows e dados compartilhados.
- RabbitMQ/Celery: filas e execucao assincrona.
- Redis: result backend, heartbeat e locks/cooldowns.
- Target Core: FileApp mailing/import/associacao e Runner v5 do `switch_bot_flow`.
- Files/Arquivos API: download, consulta, move/reupload de arquivos.
- Otima LLM: componente `intelligent_agent`.
- HTTP arbitrario: componente `api_call`.
- Identidade.io: consulta de pessoa por CPF no card `identidade_person`, com URL fixa e Bearer do card.
- SFTP/Paramiko: `generate_file`.
- Supplier: endpoint autenticado de `resubmit`.
- Billing: publisher confirmado no exchange `domain.events`; consumer deduplica por `snapshot_id` (premissa operacional fornecida).

## Invariantes criticas

- Com PgBouncer, `NullPool`, o cache de statements do `asyncpg` e o cache de prepared statements do dialeto SQLAlchemy devem permanecer desativados; prepared statements devem ser anonimos para impedir reutilizacao de plano entre clientes/workspaces no backend compartilhado. Migrations externas podem invalidar planos sem derrubar o health de API/Celery.
- `entity_origin_app` e origem historica; o evento corrente esta em `runtime_variables.source_app` e snapshots.
- `unassigned_at IS NOT NULL` impede reuso normal.
- `finish_flow` deve deixar `state=3`, `ended_at` preenchido e `next_card_uuid=NULL`.
- Quando `finish_flow.parameters.webhook` esta configurado, o contrato externo e `session` (campos persistidos, `result` e contato normalizado uma vez) mais `cdr` (payload cru do evento Dialer do ledger). `runtime_variables` nunca integra o body. Uma sessao Dialer pode emitir um webhook por CDR distinto, pois representa tentativas sequenciais do mesmo contato; o ledger marca cada CDR confirmado e impede reenvio daquele mesmo evento.
- Cursores `last_card_uuid`/`next_card_uuid` e runtime precisam permanecer coerentes.
- FileApp nao cria rota paralela; entra na rota canonica.
- `source_list_members` nao e manipulado pelo FileApp local.
- `call_origin` da associacao FileApp e `file_event`; `linked_by` e o `file.id`.
- Migrations ORCH usam `orch_alembic_version`, nunca `alembic_version`.
- Valores de `linked_actuator_enum` pertencem ao Target Core e nao sao migrados pelo ORCH.
- Cards HSM WhatsApp materializam o payload final em `contact_list_members.outbound_hsm` na mesma transacao que define ANI/atuador. O Contact Supplier nao deve interpretar grafo ou card; deploy exige primeiro a migration Target, depois ORCH e por ultimo o cutover do Supplier.
- Billing batch conta a criacao de `orch_sessions`, usa `created_at` UTC e nunca reconstrói o payload durante retry. `sent` significa confirmacao/roteamento do RabbitMQ, nao processamento pelo consumer.
- `switch_bot_flow` envia ao provider `whatsapp` do Runner v5 o mesmo conteudo JSON recebido da Meta, inclusive no primeiro evento; nao cria envelope sintetico e nao encaminha status `sent/delivered/read/failed`. Usar `/webhook/session` fragmenta a identidade e desvia o dispatch para uma integracao webhook do flow.
- A sessao ORCH permanece bloqueada no `switch_bot_flow` ate callback terminal. O `finish_flow` BOT observado em runtime envia ao alias curto do proprio flow ORCH um envelope `entity + session.id + disposition`; `session.id` coincide com `target_session_id`. Esse envelope deve ser consumido antes do trigger comum para nao criar uma sessao fantasma. O primeiro estado terminal vence callbacks tardios conflitantes.
- `identidade_person` em `lookup_only` não pode escrever em `persons` ou listas. Telefones marcados `do_not_disturb` não podem virar canais acionáveis. O vínculo mailing→flow usa `call_origin=identidade_person` somente depois do commit local; o Target valida o card publicado, materializa membros idempotentemente e não redispara sessões.
- `manage_contact_channels` é exclusivo de `person` e opera somente após a adoção de uma pessoa. `upsert|deactivate` altera `persons.channels` de forma idempotente, sem criar lista, membro, sessão ou `linked_actuator`; o mesmo endereço pode pertencer a pessoas diferentes. A cópia para `contact_draft_channels` deve preservar `is_valid/is_reachable` para não ressuscitar canal desativado.

## Estado da baseline

### CONFIRMED

- O primeiro canário de escrita do `identidade_person` (`9ec18a2d-3807-43e2-9c2e-1db2ed4ff170`) encontrou a pessoa na Identidade.io, mas reverteu o savepoint com `identidade_person_persistence_failed`: o normalizador preservava `birthday` como string ISO e o `asyncpg` exige `datetime.date` para a coluna PostgreSQL `date`. Não houve escrita parcial nem fan-out. A correção converte a data somente na fronteira SQL, preservando a string serializável no runtime. Além da transação real revertida, o canário E2E pré-deploy `1b54233b-7075-42c9-8085-35c8afad5db7` criou pessoa, draft, 8 canais, materializou 8 membros, vinculou a lista com HTTP 200 e terminou em `state=3`; o flow ganhou exatamente uma sessão. A confirmação pós-deploy do mesmo código ainda permanece pendente.

- O envelope real de `identidade_person` usa formatos mistos da UI (string, objeto `{id, name}` e lista de checkbox); a engine os normaliza. As queries de pessoa/draft/lista foram executadas no PostgreSQL do workspace de teste dentro de transação revertida, com zero resíduos após rollback. O canário real `2dd62260-3519-45dd-9275-ad0c56359b84`, em `lookup_only`, consultou a Identidade.io uma vez, terminou em `state=3` e não criou pessoa, draft, canal ou vínculo.

- Estrutura, entrypoints, rotas, tasks, filas, profiles, migrations e componentes foram rastreados no codigo.
- A suite foi executada fora da sandbox: 295 coletados, 270 passaram, 25 falharam primeiro pela assinatura stale da rota legada; sucesso posterior desses casos nao foi comprovado.
- A unit systemd FileApp versionada nao consome a fila de associacao.
- O helper de lock do reconciliador FileApp retorna implicitamente `None` quando Redis existe.
- O dispatcher publica tasks sem commit explicito da transacao externa que fez o claim.
- O flow `0e378237-4a61-4d5f-89f3-b07b594df38f` demonstrou em runtime que erro permanente de definicao pode ser reenfileirado indefinidamente. A contenção final terminou em 1.154.025 alarmes; as sessoes `256`, `257` e `263` foram encerradas e a contagem estabilizou.
- O mesmo padrão reapareceu no flow de produção `2112aa34-0c48-4cd6-a477-d8b5f5e1f52e`: duas sessões fixadas na revisão v46 alcançaram `api_call` com URL runtime vazia. A edge `exception` existia, mas a engine relançava `api_call_missing_url`; cinco eventos WhatsApp pendentes por sessão faziam o reconciliador reenfileirá-las. A correção mínima está validada localmente e o incidente permanece ativo até deploy/observação das sessões `809`/`810`.
- `CELERY_DISPATCH_WORKSPACE_UUID` nao limita o reconciliador de eventos pendentes. Uma stack `f5_local` com dispatcher escopado, mas sem `CELERY_RECONCILE_PENDING_EVENTS_WORKSPACE_UUID`, varreu outro workspace no DB compartilhado e reativou a sessao `263`.
- `scripts/dev_phase_stack.sh status` pode reportar down enquanto subprocessos Uvicorn/Celery sobrevivem ao wrapper registrado no pidfile. Em 2026-08-24 foram encontrados processos `f5_local` stale por quase duas horas; a contencao exigiu encerramento por command line.
- O seletor legado de Dialer/WhatsApp/contexto escolhe o membro ativo mais novo por `contact_identifier`; em 2026-08-24 foram confirmadas 26 sessoes divergentes no workspace Highcomm. A correcao contextual foi habilitada no host `10.1.20.237`: 46 sessoes novas com escopo explicito produziram 46 assignments sem divergencia de membro, lista, mailing ou atuador; a sessao `6941` do flow alvo resolveu o membro `10655` e persistiu `linked_actuator=dialer`.
- Sessões criadas pelo Target Core com `session_scope=person` forçam resolução contextual mesmo se a flag histórica estiver desligada. Qualquer escopo explícito também valida o endereço da sessão e, quando informado, o tipo do canal. O ORCH continua sendo a autoridade de `linked_actuator`; o escopo `person` só pode alcançar cards de saída WhatsApp/Dialer depois de `select_contact_channel` escolher explicitamente um membro da mesma pessoa/lista/mailing. Antes disso, o guard terminaliza a sessão.
- O flow `4d81d73b-dfee-43b8-9c82-d3c52207941f` demonstrou a variante silenciosa: 4.389.386 metricas de executor com `blocked_send_whatsapp_interactive`, sete sessoes `state=0` e nenhum alarme do flow na fotografia de 2026-08-24 15:28 BRT. A omissao do motivo na transicao defensiva foi corrigida no branch `fix/blocked-whatsapp-interactive-loop`, ainda sem deploy.
- No snapshot pos-migracao, API, tres beats e quinze workers ORCH estavam ativos no `10.1.20.237`, sem restart de unit; as oito filas ORCH tinham cinco consumers e zero mensagens prontas. Os workers/beats ORCH permaneceram desabilitados no `10.1.20.136`, cuja API continuou ativa por restricao temporaria do proxy.
- Tres beats no `10.1.20.237` publicavam schedules sobrepostos: pending-channel reconcile em dois beats e FileApp post-process/rescue em tres. Os arquivos de schedule eram distintos; a duplicacao vinha das flags herdadas pelo mesmo `celery_app`.
- O rescue/higiene FileApp pede somente a primeira pagina da API de Arquivos. Com `*_BATCH_SIZE=2` e listagem decrescente, o flow `652ee631-888e-46f9-843e-d80543051801` deixou arquivos antigos sem evento, ingestao ou quarentena por starvation; consultar `KNOWN_RISKS.md` R25 antes de alterar os reconciliadores.
- O `PUT /v2/mailings/{id}/field-mappings` do Target Core pode autoenfileirar a ingestao e responder `INGESTING` ou `PROCESSED`. Tratar esses estados como falha no step 5 deixa o arquivo na entrada ate o rescue; consultar R26 antes de alterar o pipeline Tipo 1.
- `/health/celery` considera qualquer worker do vhost compartilhado como prova de `worker_ok`; `/health/ready` valida somente DB/schema. Ambos podem permanecer verdes sem workers ORCH.
- Reciclagem SIGTERM de child process expos `UnboundLocalError` em `_advance_session_task`: `stopped_reason` pode ser lido no `finally` antes de ser inicializado, mascarando a excecao original.
- `live` nao e suportado no branch atual nem em `main`; o commit isolado `bd461a5` nao foi integrado e sua implementacao nao executa handoff ou callback externo.
- O smoke versionado valida aceite HTTP, nao conclusao E2E.
- O billing batch foi validado em PostgreSQL real com tabelas temporarias: 450 sessoes produziram snapshots `200 + 200 + 50`. Reprocessamento mensal usa chunks persistentes de 1000 e retomada por cursor. Broker/consumer alvo, concorrencia multi-transacao e rollout permanecem `UNKNOWN`; a migration `0022` exige medicao do lock do novo indice temporal no LAB.
- O novo card `switch_bot_flow` e implementado sem alterar `run_flow`; consulta do `runner_token`, fila isolada e regressao direcionada foram validadas. O canario real confirmou Meta -> ORCH -> BOT -> Meta com identidade Runner estavel e respostas WhatsApp. O primeiro callback terminal chegou ao alias curto, mas entrou no trigger comum e criou a sessao fantasma `7106`; a correcao Alpha passa a correlaciona-lo por `session.id == target_session_id` antes da persistencia normal.

### LIKELY

- Enqueues duplicados podem ocorrer enquanto claims do dispatcher sao revertidos ou antes do commit do request.
- O storm silencioso do WhatsApp e fortemente compativel com claim revertido + scan periodico, agravado pela ausencia do stop reason na transicao defensiva do dispatcher.
- `generate_file` pode repetir efeito SFTP se houver crash entre upload e commit.

### UNKNOWN

- Commit efetivo de cada worker que processou o flow de WhatsApp e origem exata de cada uma das tarefas repetidas.
- Protecao de proxy/ingress, TLS e autorizacao externa.
- Cobertura real das migrations em cada workspace e drift de schema.
- Saude funcional atual de Target Core, Files API, LLM e SFTP; a auditoria confirmou apenas conectividade da API com PostgreSQL, RabbitMQ e Redis.
- Se o Target Core produz `persons + orch_sessions` para todo FileApp `tipo_1`.
- Ultima evidencia E2E completa de FileApp, `api_call` e `generate_file`.
- Idempotencia efetiva do Runner v5 por `messages[].id` e protecao de ingress dos callbacks.

## Indice de conhecimento

- `docs/project-knowledge/ARCHITECTURE.md`
- `docs/project-knowledge/COMPONENTS.md`
- `docs/project-knowledge/DATA_FLOW.md`
- `docs/project-knowledge/DATABASE.md`
- `docs/project-knowledge/DEPENDENCIES.md`
- `docs/project-knowledge/EXTERNAL_INTEGRATIONS.md`
- `docs/project-knowledge/CONFIGURATION.md`
- `docs/project-knowledge/PRODUCTION_RUNBOOK.md`
- `docs/project-knowledge/KNOWN_QUIRKS.md`
- `docs/project-knowledge/KNOWN_RISKS.md`
- `docs/project-knowledge/TECHNICAL_DEBT.md`
- `docs/project-knowledge/MAINTENANCE_LOG.md`
- `docs/project-knowledge/INCIDENT_HISTORY.md`
- `docs/project-knowledge/POSTERGADO_SUPPLIER_V2_DIAL_RULE.md` — plano integral, atualmente postergado, do Supplier V2 isolado com retentativa governada pela Dial Rule selecionada no flow; deve ser adaptado após o novo CRUD de Dial Rules.
- `docs/project-knowledge/DIALING_MANAGEMENT_V2_IMPLEMENTATION_PLAN.md` — contrato, persistência, migration, controladores e evolução segura da UI operacional de Gestão de Extensões no `10.1.20.239`.
- `docs/project-knowledge/DIALING_MANAGEMENT_UI_RUNBOOK.md` — fonte, runtime Node 22, build, deploy atômico, smoke e rollback da UI operacional de Gestão de Extensões no `10.1.20.239`.
- `docs/project-knowledge/MULTI_DIALER_EXECUTION_PLAN.md` — fonte única da verdade para N cards do novo Dialer por flow, com contratos, gates T1–H1, evidências, rollout fail-closed e retorno obrigatório ao flow completo.
- `docs/project-knowledge/FLOW_SESSION_SCOPE_CONTRACT.md` — contrato normativo Person/Channel, seleção de canal, Dial Rule, validações 422 e ordem segura para retomar o canário multidialer e o flow completo.
- `docs/project-knowledge/JOURNEY_TRACKING.md` — contrato read-only, privacidade, guardrails, UI, rollout e retorno ao flow completo do Rastreamento de Jornadas.
- `docs/project-knowledge/CONTACT_CHANNEL_MANAGEMENT.md` — contrato, normalização, idempotência, projeção primária legada, segurança, testes e sequência de homologação do card genérico de canais.
