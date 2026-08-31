# ORCH — Project Brain

Baseline inicial de sustentacao criada em 2026-08-24 conforme `PROJECT_STEWARD.md`.

Esta memoria descreve o comportamento confirmado no repositorio. Ela nao comprova, por si so, o estado atualmente implantado em producao. Use os marcadores:

- `CONFIRMED`: comprovado por codigo executado, testes, configuracao versionada ou historico Git.
- `LIKELY`: evidencias fortes, ainda sem confirmacao de runtime.
- `UNKNOWN`: depende de ambiente, dados ou sistemas externos nao observados.

## READ THIS BEFORE CHANGING ANYTHING

1. Este repositorio e um Alpha em producao. A regra e `STABILITY OVER ELEGANCE` e a mudanca padrao e `MINIMUM SAFE CHANGE`.
2. A rota canonica e `POST /v1/orch/{workspace_uuid}/{flow_uuid}`. O `workspace_uuid` seleciona o schema `ws_<uuid>` e deve estar ativo/completo.
3. Nao misture stack manual e `launchd`. Em DEV, use `scripts/dev_phase_stack.sh`. Em producao, o host canonico e `10.1.20.237`, com runtime em `/etc/gohp/orch` e 19 units systemd escaladas; acesso, credencial e inventario ficam em `PROJECT_STEWARD.md`. Os templates genericos de `systemctl/` nao representam literalmente essa instalacao.
4. Filas sao contrato operacional. Use `ORCH_QUEUE_PROFILE` e filas isoladas; nunca reutilize filas de outras aplicacoes sem ordem explicita.
5. FileApp decide `tipo_1` ou `tipo_2` pela resolucao de `mapping_template`. `tipo_1` delega a importacao ao Target Core; `tipo_2` persiste sessoes no ORCH. O efeito final `persons + orch_sessions` do `tipo_1` ainda exige comprovacao E2E externa.
6. O codigo atual nao implementa autenticacao para trigger, consultas ou endpoints admin de migration. Protecao externa e `UNKNOWN`.
7. O risco de amplificacao deixou de ser apenas estatico: em 2026-08-24, sessoes invalidas de um flow draft acumularam 1.154.025 falhas. As sessoes `256`, `257` e `263` foram terminalizadas de forma auditada; consulte `docs/project-knowledge/INCIDENT_HISTORY.md` antes de intervir em dispatcher, reconciliador, filas ou sessoes.
8. Bloqueios considerados sucesso tambem podem ser amplificados sem alarme. A auditoria posterior a migracao dos workers para o host `10.1.20.237` confirmou o loop `blocked_send_whatsapp_interactive` ativo em tres workspaces, mais de 213 mil execucoes de executor e 428 mil metricas em cerca de 70 minutos. A correcao Alpha inclui esse motivo em `BLOCKING_RUNNING_STOP_REASONS`, preservando a sessao em `state=1` ate callback/reconciliacao; implantacao e validacao de runtime ainda estao pendentes.
9. A suite coleta 295 testes. Em 2026-08-24, 270 passaram e 25 pararam primeiro porque testes antigos chamam a rota legada com o parametro removido `flow_uuid`; corrigir apenas a assinatura pode revelar divergencias semanticas adicionais. Nao trate a suite como verde.
10. Nao conclua runtime apenas por leitura ou teste unitario. Fluxos com DB, broker, API externa ou SFTP exigem evidencia fora da sandbox.

## O que e o ORCH

O ORCH recebe eventos externos heterogeneos, identifica sua origem, correlaciona ou cria sessoes por workspace, carrega uma definicao `flow_v2`, executa cards do workflow e coordena efeitos assincronos por Celery.

Aplicacoes detectadas, nesta ordem: `ArquivosApp`, `WhatsApp`, `DialerApp`, `GenericApp`.

## Runtime confirmado no repositorio

| Processo | Papel | Filas logicas |
|---|---|---|
| API FastAPI/Uvicorn | health, triggers, consultas, admin e enqueue | publica `execute` e FileApp ingest |
| Worker workflow | dispatch, execucao e heartbeat | `dispatch`, `execute`, `heartbeat` |
| Beat workflow | dispatch, heartbeat e reconciliacoes | publica nas filas acima e FileApp process |
| Worker FileApp | ingest, process, associacao e reconciliacao | `fileapp_ingest`, `fileapp_process`, `fileapp_mailing_assoc` |
| Worker generate_file | scan e envio SFTP | `generate_file_scan`, `generate_file_run` |
| Beat generate_file | scan periodico | publica `generate_file_scan` |

Entrypoints:

- API: `app.main:app`.
- Celery: `app.core.celery_app:celery_app`.
- CLI: `python -m app.cli migrate-all` e `python -m app.cli migrate-workspace <uuid>`.
- DEV: `scripts/dev_phase_stack.sh`.

## Principais fluxos

- Trigger comum: valida workspace -> detecta app -> correlaciona/persiste sessao -> registra eventos de canal -> bootstrap -> enqueue/executa M2 -> `202`.
- Workflow: seleciona revisao -> injeta runtime -> executa cards sob lock da sessao -> persiste cursores -> finaliza, pausa ou bloqueia.
- FileApp `tipo_1`: valida pasta/template -> Celery ingest/process -> Target Core upload/mapping/import -> task de associacao -> pos-processamento do arquivo.
- FileApp `tipo_2`: persiste sessao do arquivo -> baixa/expande CSV -> processa cada linha pelo trigger comum.
- Canal/callback: correlaciona sessao ativa ou recente -> registra ledger/runtime -> retoma card bloqueante; sem correlacao, audita descarte.
- Generate file: card grava job/buffer -> beat scan -> worker produz arquivo SFTP -> auditoria e runtime.

Detalhes: `docs/project-knowledge/DATA_FLOW.md`.

## Persistencia

Objetos ORCH por schema de workspace:

- `orch_sessions`, `orch_sessions_alarms`, `orch_session_metrics`, `orch_discarded_events`, `orch_channel_events`;
- `orch_generate_file_job`, `orch_generate_file_row_buffer`, `orch_generate_file_dispatch_audit`;
- `orch_whatsapp_limits`, `orch_whatsapp_rate_limit_per_flow`;
- `orch_alembic_version`.

Objeto central criado pelo ORCH: `target.orch_flow_aliases`.

Objetos compartilhados consumidos ou alterados em fluxos especificos: `target.workspaces`, `flow_v2`, `flow_v2_revision`, `source_lists`, `persons`, `contact_list_members`, `cache_card_store`.

Detalhes e ownership: `docs/project-knowledge/DATABASE.md`.

## Integracoes externas

- PostgreSQL/PgBouncer: estado, flows e dados compartilhados.
- RabbitMQ/Celery: filas e execucao assincrona.
- Redis: result backend, heartbeat e locks/cooldowns.
- Target Core: FileApp mailing/import/associacao.
- Files/Arquivos API: download, consulta, move/reupload de arquivos.
- Otima LLM: componente `intelligent_agent`.
- HTTP arbitrario: componente `api_call`.
- SFTP/Paramiko: `generate_file`.
- Supplier: endpoint autenticado de `resubmit`.

## Invariantes criticas

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

## Estado da baseline

### CONFIRMED

- Estrutura, entrypoints, rotas, tasks, filas, profiles, migrations e componentes foram rastreados no codigo.
- A suite foi executada fora da sandbox: 295 coletados, 270 passaram, 25 falharam primeiro pela assinatura stale da rota legada; sucesso posterior desses casos nao foi comprovado.
- A unit systemd FileApp versionada nao consome a fila de associacao.
- O helper de lock do reconciliador FileApp retorna implicitamente `None` quando Redis existe.
- O dispatcher publica tasks sem commit explicito da transacao externa que fez o claim.
- O flow `0e378237-4a61-4d5f-89f3-b07b594df38f` demonstrou em runtime que erro permanente de definicao pode ser reenfileirado indefinidamente. A contenção final terminou em 1.154.025 alarmes; as sessoes `256`, `257` e `263` foram encerradas e a contagem estabilizou.
- `CELERY_DISPATCH_WORKSPACE_UUID` nao limita o reconciliador de eventos pendentes. Uma stack `f5_local` com dispatcher escopado, mas sem `CELERY_RECONCILE_PENDING_EVENTS_WORKSPACE_UUID`, varreu outro workspace no DB compartilhado e reativou a sessao `263`.
- `scripts/dev_phase_stack.sh status` pode reportar down enquanto subprocessos Uvicorn/Celery sobrevivem ao wrapper registrado no pidfile. Em 2026-08-24 foram encontrados processos `f5_local` stale por quase duas horas; a contencao exigiu encerramento por command line.
- O seletor legado de Dialer/WhatsApp/contexto escolhe o membro ativo mais novo por `contact_identifier`; em 2026-08-24 foram confirmadas 26 sessoes divergentes no workspace Highcomm. A correcao contextual foi habilitada no host `10.1.20.237`: 46 sessoes novas com escopo explicito produziram 46 assignments sem divergencia de membro, lista, mailing ou atuador; a sessao `6941` do flow alvo resolveu o membro `10655` e persistiu `linked_actuator=dialer`.
- O flow `4d81d73b-dfee-43b8-9c82-d3c52207941f` demonstrou a variante silenciosa: 4.389.386 metricas de executor com `blocked_send_whatsapp_interactive`, sete sessoes `state=0` e nenhum alarme do flow na fotografia de 2026-08-24 15:28 BRT. A omissao do motivo na transicao defensiva foi corrigida no branch `fix/blocked-whatsapp-interactive-loop`, ainda sem deploy.
- No snapshot pos-migracao, API, tres beats e quinze workers ORCH estavam ativos no `10.1.20.237`, sem restart de unit; as oito filas ORCH tinham cinco consumers e zero mensagens prontas. Os workers/beats ORCH permaneceram desabilitados no `10.1.20.136`, cuja API continuou ativa por restricao temporaria do proxy.
- Tres beats no `10.1.20.237` publicavam schedules sobrepostos: pending-channel reconcile em dois beats e FileApp post-process/rescue em tres. Os arquivos de schedule eram distintos; a duplicacao vinha das flags herdadas pelo mesmo `celery_app`.
- `/health/celery` considera qualquer worker do vhost compartilhado como prova de `worker_ok`; `/health/ready` valida somente DB/schema. Ambos podem permanecer verdes sem workers ORCH.
- Reciclagem SIGTERM de child process expos `UnboundLocalError` em `_advance_session_task`: `stopped_reason` pode ser lido no `finally` antes de ser inicializado, mascarando a excecao original.
- `live` nao e suportado no branch atual nem em `main`; o commit isolado `bd461a5` nao foi integrado e sua implementacao nao executa handoff ou callback externo.
- O smoke versionado valida aceite HTTP, nao conclusao E2E.

### LIKELY

- Enqueues duplicados podem ocorrer enquanto claims do dispatcher sao revertidos ou antes do commit do request.
- O storm silencioso do WhatsApp e fortemente compativel com claim revertido + scan periodico, agravado pela ausencia do stop reason na transicao defensiva do dispatcher.
- `generate_file` pode repetir efeito SFTP se houver crash entre upload e commit.
- Uma revisao de flow publicada entre passos pode causar drift da definicao executada.

### UNKNOWN

- Commit efetivo de cada worker que processou o flow de WhatsApp e origem exata de cada uma das tarefas repetidas.
- Protecao de proxy/ingress, TLS e autorizacao externa.
- Cobertura real das migrations em cada workspace e drift de schema.
- Saude funcional atual de Target Core, Files API, LLM e SFTP; a auditoria confirmou apenas conectividade da API com PostgreSQL, RabbitMQ e Redis.
- Se o Target Core produz `persons + orch_sessions` para todo FileApp `tipo_1`.
- Ultima evidencia E2E completa de FileApp, `api_call` e `generate_file`.

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
