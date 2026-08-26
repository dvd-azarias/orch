# Riscos Conhecidos

Baseline estatica de 2026-08-24. Nenhum destes riscos foi corrigido durante o onboarding.

## R1 — Claims do dispatcher sem commit externo

`STATUS`: FIX IMPLEMENTED / DEPLOY AND RUNTIME VALIDATION PENDING

`IMPACT`: high

`PROBABILITY`: high quando o dispatcher periodico e usado

`AFFECTED AREA`: Celery workflow / PostgreSQL

`DESCRIPTION`: a task lista workspaces, abre transacao implicita, faz claim em savepoint e publica task, mas fecha a sessao sem commit explicito do outer transaction. Claims e metricas podem ser revertidos enquanto o enqueue permanece.

`RUNTIME EVIDENCE`: em 2026-08-24, o flow `0e378237-4a61-4d5f-89f3-b07b594df38f` mantinha tres sessoes no cursor inicial e acumulava 1.136.779 alarmes de execucao. Falhas permanentes reapareciam aproximadamente no ritmo do dispatcher. A correlacao exata task/worker nao foi preservada, mas o comportamento e compativel com o claim revertido e enqueue externo.

`MITIGATION`: advisory lock no executor limita execucao simultanea; nao elimina enqueue repetido nem perda de metricas.

`DETECTION`: comparar logs de claimed/enqueued com estado e metricas; observar duplicacao de task por session id.

`V2`: usar outbox/claim atomico com commit antes de publish ou broker transactional pattern.

## R2 — Lock Redis do pos-processamento FileApp quebrado

`STATUS`: CONFIRMED STATIC

`IMPACT`: high

`PROBABILITY`: high quando Redis backend existe e reconciliador roda

`AFFECTED AREA`: FileApp reconcile post-process

`DESCRIPTION`: `_try_acquire_fileapp_post_process_lock` retorna `True` apenas sem Redis e retorna implicitamente `None` com Redis; o bloco de lock ficou inalcançavel em outro helper. Candidatos sao ignorados.

`MITIGATION`: caminho principal tenta pos-processar inline; reconciliador nao pode ser considerado recovery confiavel.

`DETECTION`: candidatos elegiveis sem movimento; reconciliador reporta zero apesar de registros; teste direto do helper retorna `None`.

`V2`: helper testado sem monkeypatch e lock encapsulado.

## R3 — Fila de associacao sem consumidor systemd

`STATUS`: CONFIRMED TEMPLATE / RUNTIME UNKNOWN

`IMPACT`: high

`PROBABILITY`: high se producao usa a unit versionada sem override

`AFFECTED AREA`: FileApp tipo 1 / Celery / systemd

`DESCRIPTION`: producer roteia para `orch_fileapp_mailing_assoc`; a unit FileApp consome somente ingest e process.

`MITIGATION`: launchd e stack DEV incluem a fila. Um consumidor adicional manual pode existir.

`DETECTION`: inspecionar command line/Flower/RabbitMQ e backlog da fila; correlacionar mailings importados sem vinculo.

`V2`: manifest unico de processos/filas gerado e validado em CI.

## R4 — Schedules duplicados entre beats

`STATUS`: CONFIRMED RUNTIME

`IMPACT`: high

`PROBABILITY`: high enquanto os tres beats compartilharem as flags atuais

`AFFECTED AREA`: Celery Beat

`DESCRIPTION`: beat generate-file desabilita dispatch/heartbeat, mas herda reconcile de canal e pos-process FileApp. O beat principal e o beat FileApp tambem herdam schedules FileApp. No host `10.1.20.237`, pending-channel reconcile foi publicado por dois beats; FileApp post-process e entrada-rescue foram publicados pelos tres beats, todos escopados ao mesmo workspace FileApp.

`RUNTIME EVIDENCE`: desde a subida conjunta, o beat principal e o generate-file publicaram 251 reconciliacoes de eventos cada. O beat principal, o FileApp e o generate-file publicaram 62 post-process e 62 entrada-rescue cada. Os schedule files sao distintos; nao houve disputa do arquivo local. O excesso e de publishers/tasks.

`MITIGATION`: definir ownership explicito por schedule e desabilitar nos outros beats todas as flags nao pertencentes ao processo. Locks/cooldowns reduzem efeitos de negocio, mas nao eliminam publicacao, consumo e ruido duplicados.

`DETECTION`: comparar logs de ambos os beats e task IDs por schedule.

`V2`: apps/schedules separados por processo.

## R5 — Perimetro de autenticacao nao versionado

`STATUS`: CONFIRMED CODE / EXTERNAL UNKNOWN

`IMPACT`: critical se endpoints estiverem expostos

`PROBABILITY`: unknown

`AFFECTED AREA`: API, inclusive migrations admin

`DESCRIPTION`: somente resubmit tem auth propria. Trigger, consultas e migrations nao implementam auth no app; systemd escuta em `0.0.0.0` com proxy headers amplos.

`MITIGATION`: ACL de docs; gateway/proxy pode proteger, mas nao foi comprovado.

`DETECTION`: revisar ingress/proxy/firewall e testar acesso autorizado/nao autorizado.

`V2`: authn/authz explicita por rota e defense in depth.

## R6 — Efeitos externos dentro de transacao

`STATUS`: CONFIRMED STATIC

`IMPACT`: high

`PROBABILITY`: medium

`AFFECTED AREA`: `api_call`, LLM, SFTP, FileApp

`DESCRIPTION`: chamadas externas ocorrem antes do commit. Falha posterior pode reexecutar efeito que nao pode ser revertido.

`MITIGATION`: alguns retries e locks; idempotencia externa varia.

`DETECTION`: efeitos duplicados com transacao sem estado correspondente.

`V2`: outbox, idempotency keys e etapas curtas.

## R7 — Drift de revisao de workflow

`STATUS`: CONFIRMED STATIC, IMPACT LIKELY

`IMPACT`: high

`PROBABILITY`: low/medium

`AFFECTED AREA`: workflow M1/M2

`DESCRIPTION`: bootstrap registra revisao, mas M2 seleciona novamente a revisao corrente. Publicacao entre passos pode invalidar cursores/semantica.

`MITIGATION`: nenhuma pinagem confirmada.

`DETECTION`: comparar revision id do runtime/metricas com revisao carregada na execucao.

`V2`: pin de revisao por sessao.

## R8 — FileApp tipo 1 depende de efeito externo nao comprovado

`STATUS`: UNKNOWN RUNTIME

`IMPACT`: high

`PROBABILITY`: unknown

`AFFECTED AREA`: FileApp / Target Core / dados

`DESCRIPTION`: ORCH nao escreve diretamente `persons`/`orch_sessions`; cumprimento da invariante depende do Target Core e associacao.

`MITIGATION`: cadeia de APIs e testes unitarios com mocks.

`DETECTION`: E2E com POST observado e SQL final.

`V2`: contrato de integracao testado e ownership explicito.

## R9 — Configuracao pode alterar semantica FileApp

`STATUS`: CONFIRMED STATIC

`IMPACT`: high

`PROBABILITY`: medium

`AFFECTED AREA`: FileApp/API

`DESCRIPTION`: com Celery ou FileApp ingest desabilitado, evento com template pode cair no processamento local tipo 2. Template invalido tambem pode ser tratado como ausencia.

`MITIGATION`: manter flags corretas; nao ha fail-closed.

`DETECTION`: resposta sem pipeline tipo1 e escrita ORCH para evento que traz template.

`V2`: decisao de negocio separada do modo de execucao.

## R10 — Suite de regressao parcialmente stale

`STATUS`: CONFIRMED

`IMPACT`: medium/high

`PROBABILITY`: high para manutencoes sem selecao de testes

`AFFECTED AREA`: QA

`DESCRIPTION`: 25 de 295 testes param primeiro na assinatura antiga da rota legada. Revisao adversarial encontrou tambem expectativa Dialer possivelmente stale; corrigir o argumento nao garante suite verde.

`MITIGATION`: 270 casos passam; suites focadas podem ser usadas com cautela.

`DETECTION`: `pytest -q` fora da sandbox.

`V2`: testes por contrato HTTP e separacao unit/integration.

## R11 — Health e smoke podem produzir falso conforto

`STATUS`: CONFIRMED STATIC

`IMPACT`: medium

`PROBABILITY`: high

`AFFECTED AREA`: operacao

`DESCRIPTION`: health Celery aceita qualquer worker; smoke nao espera fim nem efeito externo.

`MITIGATION`: validar consumers, banco e destino manualmente.

`DETECTION`: comparar health verde com filas sem consumidor ou sessoes paradas.

`V2`: readiness por capacidade/fila e smoke E2E.

## R12 — Instalacao systemd pode sobrescrever ambiente

`STATUS`: CONFIRMED STATIC

`IMPACT`: high

`PROBABILITY`: medium se `install` for executado

`AFFECTED AREA`: deployment

`DESCRIPTION`: script de install copia `orch.env.example` para o caminho ativo sem preservacao explicita.

`MITIGATION`: nao executar sem backup/revisao.

`DETECTION`: diff/mtime do env antes e depois.

`V2`: install idempotente e fail-safe.

## R13 — FileApp pode mover arquivo sem associacao garantida

`STATUS`: CONFIRMED STATIC

`IMPACT`: high

`PROBABILITY`: medium

`AFFECTED AREA`: FileApp tipo 1

`DESCRIPTION`: falha ao enfileirar `associate_mailing` vira warning/alarme, mas o fluxo continua para mover o arquivo a `processados` e retorna `done`. Configuracao de associacao que desapareca entre import e task tambem pode resultar em `ignored` sem retry.

`MITIGATION`: reconciliador de pos-processamento existe, mas seu lock Redis esta quebrado na baseline atual.

`DETECTION`: arquivo em `processados`, mailing importado sem vinculo, alarme de enqueue e ausencia de task de associacao.

`V2`: estado persistente por etapa e conclusao somente apos associacao confirmada ou recovery duravel.

## R14 — FileApp detach pode remover mailings anteriores

`STATUS`: CONFIRMED STATIC / CONTRACT CONFLICT

`IMPACT`: high

`PROBABILITY`: unknown

`AFFECTED AREA`: FileApp associacao

`DESCRIPTION`: quando `detach_all_files=true`, o body inclui mailings anteriores em `mailing_ids_removed`, contrariando a invariante atual de lista vazia em `AGENTS.md`.

`MITIGATION`: comportamento coberto por teste, mas sem reconciliacao documental da regra de negocio.

`DETECTION`: inspecionar definicao do flow e payload efetivamente enviado ao Target Core.

`V2`: contrato explicito e versionado para estrategia add/remove.

## R15 — FileApp nao possui claim duravel por file.id

`STATUS`: CONFIRMED STATIC, EXTERNAL IDEMPOTENCY UNKNOWN

`IMPACT`: high

`PROBABILITY`: medium sob redelivery/concorrencia

`AFFECTED AREA`: FileApp tipo 1

`DESCRIPTION`: entrada normal publica sem idempotency claim persistente por workspace/flow/file. Guards posteriores e nomes incrementais nao impedem uploads concorrentes.

`MITIGATION`: tratamento parcial de conflito de import e guards apos movimentacao.

`DETECTION`: mais de um mailing/source list para o mesmo `file.id`, tasks concorrentes e nomes incrementais proximos.

`V2`: idempotency key persistente e contrato com Target Core.

## R16 — Definicao invalida pode executar e entrar em retry permanente

`STATUS`: CONFIRMED RUNTIME / INCIDENT CONTAINED / FIX PREPARED

`IMPACT`: critical

`PROBABILITY`: high quando flow invalido possui sessao pendente

`AFFECTED AREA`: workflow validation / dispatcher / executor / observabilidade

`DESCRIPTION`: o runtime aceita flow `draft`, seleciona sua revisao draft e nao valida previamente branches obrigatorias ou configuracao minima de componentes. Excecao permanente na task nao terminaliza nem aplica backoff duravel. Com o comportamento de claim de R1, a mesma sessao pode ser enfileirada continuamente.

`RUNTIME EVIDENCE`: o flow `0e378237-4a61-4d5f-89f3-b07b594df38f` tinha condition sem `false/exception`, duas `api_call` sem URL e sessoes presas no primeiro card. As sessoes `256` e `257`, ainda elegiveis ao dispatcher, foram terminalizadas em 2026-08-24 16:15 BRT. Durante a validacao, um reconciliador local sem escopo reenfileirou a sessao WhatsApp `263`, `state=2`, que falhou em `api_call_missing_url`; ela foi terminalizada as 16:50 BRT. A contagem final estabilizou em 1.154.025 alarmes.

`MITIGATION`: nao publicar/acionar o flow; preservar evidencias e isolar dispatcher/sessoes somente por procedimento aprovado. A correcao preparada terminaliza `condition_branch_not_mapped` com `state=3`, `ended_at`, cursor nulo, metadado de falha e alarme unico; ainda depende de deploy validado para proteger novas sessoes.

`DETECTION`: validar grafo/config antes de publish, agregar alarmes por `flow_uuid/session_id/exception_message` e alertar para repeticao de erro permanente.

`V2`: validacao fail-closed, revisao publicada obrigatoria e politica explicita de terminalizacao/backoff/DLQ.

## R17 — Bloqueio WhatsApp pode virar retry storm silencioso

`STATUS`: CONFIRMED STATIC / AMPLIFICATION OBSERVED IN RUNTIME

`IMPACT`: critical

`PROBABILITY`: high para sessoes pendentes bloqueadas pelo template/interativo

`AFFECTED AREA`: workflow dispatcher / WhatsApp / metricas / broker

`DESCRIPTION`: `send_whatsapp_template` compartilha o caminho de `send_whatsapp_interactive` e persiste `blocked_send_whatsapp_interactive` como sucesso. O codigo implantado durante o incidente nao reconhecia esse motivo em `BLOCKING_RUNNING_STOP_REASONS`; por isso, o dispatcher nao aplicava a transicao defensiva para `state=1`. Com o claim nao duravel de R1, a sessao podia permanecer `state=0`, ser selecionada a cada scan e retornar imediatamente o mesmo bloqueio, sem reenviar a mensagem e sem produzir alarme.

`RUNTIME EVIDENCE`: em 2026-08-24 15:28 BRT, o flow `4d81d73b-dfee-43b8-9c82-d3c52207941f` tinha sete sessoes GenericApp `state=0` bloqueadas no card de WhatsApp e 4.389.386 metricas de executor `success/blocked_send_whatsapp_interactive`. A auditoria do host `10.1.20.237`, entre 19:22 e aproximadamente 20:32 BRT, confirmou a amplificacao ainda ativa em tres workspaces: mais de 213 mil execucoes de executor e 428 mil metricas novas. As sessoes quentes continuavam `state=0`, `ended_at=NULL`, `unassigned_at=NULL` e com cursor seguinte. O journal recebeu cerca de 1,27 milhao de linhas/234 MB nessa janela. O banco ja armazenava aproximadamente 199,4 milhoes de linhas e mais de 90 GB em `orch_session_metrics` nos doze schemas com dados.

`MITIGATION`: a correcao Alpha adiciona `blocked_send_whatsapp_interactive` ao conjunto bloqueante do dispatcher. A primeira execucao corrigida persiste `state=1`, removendo a sessao dos scans de `state=0`; callbacks e o reconciliador continuam podendo enfileirar a retomada diretamente. Ate o deploy e a validacao em runtime, preservar evidencias e isolar sessoes/dispatcher apenas por procedimento operacional controlado. Nao usar contagem de alarmes como unico detector.

`DETECTION`: agregar `orch_session_metrics` por `flow_uuid`, `session_id`, `stopped_reason` e janela; alertar para repeticao de bloqueio sem evento pendente e para crescimento anormal de metricas.

`V2`: estado de espera explicito, claim duravel, wake-up orientado a evento e observabilidade de loops de sucesso.

## R18 — Componente live sem runtime implantado e implementacao isolada incompleta

`STATUS`: CONFIRMED CODE / CONFIRMED RUNTIME FOR OBSERVED FLOW

`IMPACT`: high

`PROBABILITY`: high quando um flow publicado alcanca `live`

`AFFECTED AREA`: workflow M2 / atendimento humano

`DESCRIPTION`: o branch atual e `main` nao tratam `component_id=live`; o fallback produz `component_not_supported:live` e o dispatcher finaliza a sessao. O commit isolado `bd461a5`, presente apenas em `feat/live-component-orch-runtime`, reconhece o card, mas apenas registra estado local. Ele nao publica handoff nem chama `live_mirror_url`; para payload comum sem tipo `live.*`, retorna branch nula e o resolver segue a primeira edge.

`RUNTIME EVIDENCE`: a sessao `6927` do flow `4d81d73b-dfee-43b8-9c82-d3c52207941f` processou a resposta `confirmar`, concluiu a `api_call` com HTTP 200 e foi finalizada por `component_not_supported:live`. A sessao `6924` teve o mesmo stop. Foram observados 4.876 stops desse tipo no historico do flow, concentrados principalmente em uma sessao antiga.

`MITIGATION`: nao integrar `bd461a5` como correcao pronta. Primeiro confirmar o contrato do sistema Live, side effects, idempotencia, espera/resolucao e retomada; depois validar E2E em filas isoladas.

`DETECTION`: buscar `component_not_supported:live`, sessoes finalizadas apos quick reply e ausencia de handoff no destino Live.

`V2`: contrato versionado de handoff/callback com idempotencia e testes E2E.

## R19 — Escopo do dispatcher nao limita reconciliacao de eventos

`STATUS`: CONFIRMED CODE / OBSERVED IN RUNTIME

`IMPACT`: high

`PROBABILITY`: high quando DEV compartilha DB/broker com outros workspaces

`AFFECTED AREA`: Celery beat / pending channel events / isolamento operacional

`DESCRIPTION`: `CELERY_DISPATCH_WORKSPACE_UUID` filtra apenas `dispatch_pending_sessions`. A task `reconcile_pending_channel_events` usa a chave independente `CELERY_RECONCILE_PENDING_EVENTS_WORKSPACE_UUID`; quando ela esta vazia, percorre todos os workspaces concluidos. Filas locais isolam consumidores, mas nao isolam as queries nem os efeitos no DB compartilhado.

`RUNTIME EVIDENCE`: em 2026-08-24, `scripts/dev_phase_stack.sh` foi iniciado com perfil `f5_local` e dispatcher escopado ao workspace de teste. O reconciliador permaneceu global, encontrou a sessao `263` de outro workspace e a reenfileirou periodicamente na fila local, gerando 51 alarmes adicionais ate a stack ser parada e a sessao terminalizada.

`MITIGATION`: em qualquer stack conectada a DB compartilhado, definir tambem `CELERY_RECONCILE_PENDING_EVENTS_WORKSPACE_UUID` ou desabilitar explicitamente o reconciliador. Confirmar os dois escopos antes de iniciar beats; fila dedicada sozinha nao e isolamento suficiente.

`DETECTION`: logar e conferir `workspace_scope` de dispatch e reconcile, inspecionar workspaces tocados e interromper imediatamente se aparecer workspace fora do alvo.

`V2`: perfil de ambiente fail-closed que aplique um unico workspace scope a toda rotina de scan, com recusa de startup quando DEV aponta para DB compartilhado sem escopo.

## R20 — Roteamento de canal ignora a identidade da lista de origem

`STATUS`: CONFIRMED RUNTIME / FIX IMPLEMENTED BEHIND DEFAULT-OFF FLAG / RUNTIME VALIDATION PENDING

`IMPACT`: critical

`PROBABILITY`: high quando o mesmo `contact_identifier` possui mais de um membro ativo

`AFFECTED AREA`: Dialer / WhatsApp / contexto de contato / `contact_list_members`

`DESCRIPTION`: `assign_dialer_routing_for_session`, `assign_whatsapp_routing_for_session` e `fetch_contact_runtime_context_for_session` relacionam a sessao ao membro somente por `orch_sessions.entity = contact_list_members.contact_identifier`. Em seguida escolhem `ORDER BY clm.created_at DESC, clm.id DESC LIMIT 1`. `contact_list_id`, `mailing_id` e `contact_list_member_id` disponiveis no payload nao participam da selecao. Assim, um membro mais novo de outra lista pode receber `linked_actuator`/`ani`, contabilizar consumo WhatsApp e fornecer dados de contato ao workflow.

`RUNTIME EVIDENCE`: no flow `3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17`, as sessoes `6928` e `6937` declaravam `contact_list_id=dc7dc1c1-2c98-42e9-a788-5d186f458daa` e `mailing_id=1115`. O membro esperado era `10655`, mas o runtime registrou `contact_list_member_id=10687`, da lista `b5521cb2-09a9-4391-8ab5-fea25924e820`/mailing `1114`, e esse membro recebeu `linked_actuator=dialer`. O membro `10655` permaneceu `NULL`. No workspace havia 38 identificadores com duplicidade ativa, somando 113 linhas; uma agregacao conservadora encontrou 26 sessoes divergentes em tres flows: 23 Dialer e 3 WhatsApp interativo.

`MITIGATION`: a correcao implementada resolve uma vez por `contact_list_member_id`, lista ou mailing, valida cruzadamente os seletores presentes e reutiliza o mesmo membro no contexto e nos atuadores. Conflito explicito ou perda concorrente do membro terminaliza com alarme; fallback global permanece somente quando o evento nao traz escopo. A flag `WORKFLOW_CONTEXTUAL_MEMBER_ROUTING_ENABLED` e default-off e precisa de rollout controlado. Nao corrigir dados em massa sem preservar a relacao sessao/lista e validar ownership externo.

`MINIMUM SAFE CHANGE`: resolver o membro por identidade contextual compartilhada entre Dialer, WhatsApp e carregamento do contato. Priorizar `contact_list_member_id` validado; depois `contact_list_id + contact_identifier`; usar `mailing_id` apenas como qualificador. Manter o fallback legado para o membro mais novo somente quando nenhum seletor contextual foi fornecido. Se houver seletor explicito conflitante, falhar sem atualizar outra lista.

`ROLLBACK`: desligar `WORKFLOW_CONTEXTUAL_MEMBER_ROUTING_ENABLED` e reiniciar API/workers. Dados ja alterados ou sessoes terminalizadas nao sao revertidos automaticamente.

`DETECTION`: comparar `input_payload.contact_list_id` com a lista do `contact_list_member_id` persistido em `send_with_dialer_routing`, `send_with_whatsapp_routing` e `send_whatsapp_interactive_routing`.

`V2`: sessao deve persistir chave estrangeira/identidade imutavel do membro de origem; nao recorrelar por identificador de negocio a cada card.

## R21 — Stack DEV perde controle dos subprocessos e reporta falso down

`STATUS`: CONFIRMED RUNTIME

`IMPACT`: critical em ambiente compartilhado

`PROBABILITY`: high apos start/stop pelo script atual

`AFFECTED AREA`: `scripts/dev_phase_stack.sh` / Celery / isolamento operacional

`DESCRIPTION`: o script grava o PID do wrapper `bash -lc`, mas Uvicorn/Celery criam processos filhos que podem sobreviver ao encerramento do wrapper. `status` consulta somente o pidfile e pode reportar todos os componentes como `down` enquanto API, beats e workers continuam ativos. Uma nova subida reutiliza hostnames, filas e schedule files, misturando processos stale com a stack nova.

`RUNTIME EVIDENCE`: em 2026-08-24, `status` reportou toda a stack down, mas havia API e processos Celery `f5_local` orfaos desde 16:30 BRT. Nova tentativa as 18:11 BRT adicionou consumidores; os processos stale continuaram publicando dispatch, rescue e generate scan. A contencao exigiu encerrar todos os Uvicorn/Celery deste repositorio por command line. A porta 7777 e a lista de processos locais ficaram vazias; `celery inspect active_queues` confirmou nenhum consumer `f5_local`. As metricas globais do workspace continuaram crescendo pelo storm de producao ja conhecido e nao servem como criterio de shutdown local.

`MITIGATION`: antes de qualquer start, nao confiar apenas em pidfiles. Conferir porta 7777 e processos por command line; se houver stale, interromper e confirmar zero processos antes de subir. Nao repetir stack completa neste ambiente ate corrigir gerenciamento de process group, pid real, hostnames unicos e schedule files.

`DETECTION`: comparar `status` com `lsof`/process list e alertar para warning `node name already using this process mailbox`.

`V2`: supervisor unico com lifecycle verificavel, environment manifest efetivo e recusas fail-closed para DB/broker compartilhados.

## R22 — Health de Celery aceita workers alheios do broker compartilhado

`STATUS`: CONFIRMED CODE / CONFIRMED RUNTIME

`IMPACT`: high

`PROBABILITY`: high no vhost compartilhado atual

`AFFECTED AREA`: readiness / monitoramento / cutover

`DESCRIPTION`: `/health/celery` executa `inspect().ping()` sem destination/filtro e define `worker_ok` quando existe qualquer resposta. O broker observado possui muitos workers de outras aplicacoes; portanto o endpoint pode ficar verde mesmo sem nenhum worker ORCH. `/health/ready` valida apenas DB, schema e a tabela `orch_sessions`, sem compor Celery.

`RUNTIME EVIDENCE`: a resposta do host `10.1.20.237` incluiu os quinze workers ORCH e dezenas de nodes `gohp@...`/`target@...` alheios. Revisao adversarial independente confirmou que nenhum guard valida hostname, filas ou quantidade minima de workers ORCH.

`MITIGATION`: monitorar temporariamente units systemd, hostnames ORCH esperados, passive queue consumers e heartbeat em conjunto. Corrigir o health para validar o conjunto minimo por papel/fila antes de usa-lo como criterio de cutover.

`DETECTION`: comparar `worker_nodes` com o manifest esperado do host e falhar quando qualquer papel obrigatorio estiver ausente.

`V2`: health por componente, fila e deployment identity, sem depender de resposta global do vhost.

## R23 — Reciclagem de child mascara falha com `stopped_reason` indefinido

`STATUS`: CONFIRMED CODE / OBSERVED IN RUNTIME

`IMPACT`: high

`PROBABILITY`: medium durante autoscale, shutdown ou recycle

`AFFECTED AREA`: Celery workflow task / diagnostico / metricas

`DESCRIPTION`: `_advance_session_task` atribui `stopped_reason` somente dentro do `try` ou de `except Exception`, mas o `finally` sempre o persiste. `SystemExit` e `CancelledError` nao sao cobertos por esse `except`; se ocorrerem durante o primeiro await, o `finally` levanta `UnboundLocalError` e mascara a causa primaria.

`RUNTIME EVIDENCE`: childs do host `10.1.20.237` receberam SIGTERM durante conexao asyncpg. O traceback mostrou primeiro `SystemExit: -241` e depois `UnboundLocalError` em `workflow_tasks.py` ao ler `stopped_reason`. A unit permaneceu ativa e nao reiniciou; nao houve repeticao nos vinte minutos finais da auditoria. A razao exata do SIGTERM permanece desconhecida.

`MITIGATION`: inicializar estado de metrica antes do `try` e preservar/categorizar cancelamento sem mascarar a excecao original. Adicionar teste focado para `BaseException`/cancelamento antes da atribuicao.

`DETECTION`: alertar para `UnboundLocalError.*stopped_reason`, `WorkerLostError` e SIGTERM de childs; correlacionar com autoscale e limites de recycle.

`V2`: wrapper de task com outcome inicializado e lifecycle/cancelamento explicitamente modelados.

## R24 — Webhook de `finish_flow` e best-effort dentro da transacao

`STATUS`: CONFIRMED CODE

`IMPACT`: medium

`PROBABILITY`: low a medium, conforme disponibilidade e latencia do destino

`AFFECTED AREA`: executor M2 / `finish_flow` / integracao HTTP

`DESCRIPTION`: o webhook terminal usa uma chamada HTTP com timeout de 5 segundos durante a transacao do workflow. A chamada roda em thread para nao bloquear o event loop, mas a conexao e o advisory lock permanecem retidos. Nao existe outbox nem retry automatico. Uma falha mantem o CDR para diagnostico, mas a sessao termina sem nova tentativa automatica; uma interrupcao entre a resposta externa e o commit local ainda pode produzir divergencia ou repeticao, pois a marca de entrega por evento depende do commit local.

`MITIGATION`: manter o destino rapido e respeitar a `Idempotency-Key` enviada pelo ORCH, monitorar `runtime_variables.finish_flow_webhook` e reenviar manualmente quando necessario. O CDR permanece um objeto transitorio por vez na sessao, selecionado no ledger e removido da memoria somente apos `2xx`; uma sessao Dialer pode emitir um POST por CDR distinto, enquanto o marcador do ledger impede replay do mesmo evento.

`V2`: avaliar entrega transacional/idempotente fora do caminho critico caso a garantia operacional passe a exigir retry.

## R25 — Rescue e higiene FileApp podem deixar arquivos antigos em starvation

`STATUS`: CONFIRMED RUNTIME / CORRECAO IMPLEMENTADA, ROLLOUT PENDENTE (2026-08-26)

`IMPACT`: high

`PROBABILITY`: high quando a pasta recebe arquivos continuamente e o batch e menor que o backlog

`AFFECTED AREA`: FileApp entrada rescue/hygiene / Arquivos API

`DESCRIPTION`: o risco possui dois mecanismos confirmados. O primeiro era a listagem parcial: `_list_files_in_folder` solicitava somente a primeira pagina (`offset=0`) e os reconciliadores usavam o `*_BATCH_SIZE` como `limit`; com a API em ordem decrescente, itens antigos nunca eram avaliados. O segundo permanece no rescue mesmo apos a paginacao: com batch `2`, dois itens antigos em estado Redis `in_flight`/`done` continuam ocupando toda selecao. Alem disso, `_has_fileapp_ingest_evidence` considera a mera existencia de `arquivos_s3_events` como evidencia suficiente e marca o flow como `done`, embora esse recibo possa comprovar somente entrega ao Orch e ainda nao existir `source_list`. O arquivo fisico permanece na entrada e bloqueia os seguintes.

`RUNTIME EVIDENCE`: em 2026-08-26, a pasta `monitoramento/upload` continha 32 arquivos. A primeira pagina trazia `create_customer-5103-5103.csv` e os seguintes mais novos; os mais antigos, `create_customer-4699-4699.csv` e `create_customer-4701-4701.csv`, criados por volta de `09:50 UTC`, permaneciam sem qualquer evidencia no banco. Logs confirmaram repetidamente `files_scanned=2` e `skipped_recent=2`.

Na validacao posterior do recibo imediato, 31 arquivos fisicos permaneceram na entrada. Os dois mais antigos estavam `accepted + in_flight`, sem atualizacao por mais de duas horas, e causavam starvation. Apos liberar os estados stale e reprocessar, dois itens sem `source_list` foram marcados `done` pelo rescue apenas por evidencia externa. O reenvio pela rota oficial idempotente reclamou os receipts falhos; um lote final de 15 arquivos obteve `202 queued`, terminou com 15 receipts `completed`, 15 arquivos em `processados`, zero em `falha` e zero restante na entrada.

`MITIGATION`: a paginacao/ordenacao pelos mais antigos ja foi corrigida. O patch seguinte remove `arquivos_s3_events` da evidencia de ingestao, reclama `accepted` stale apos 60 segundos, faz o receipt duravel prevalecer sobre o estado Redis e trata o batch como limite de acoes; skips nao impedem a avaliacao dos itens seguintes. O rollout de producao e a validacao com arquivo real ainda estao pendentes. Ate la, a recuperacao controlada deve usar a rota oficial do Orch, que reclama apenas receipts falhos e preserva a idempotencia por `(flow_uuid, file_id)`.

`DETECTION`: alarme quando a idade do arquivo mais antigo de uma pasta monitorada ultrapassar o SLA sem evento/source list; registrar `oldest_created_at`, pagina/offset e contagem de itens elegíveis por ciclo.

`V2`: claim persistente por arquivo e cursor/paginação durável, desacoplados da ordenação externa da listagem.
