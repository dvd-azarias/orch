# Riscos Conhecidos

Baseline estatica de 2026-08-24. Nenhum destes riscos foi corrigido durante o onboarding.

## R1 — Claims do dispatcher sem commit externo

`STATUS`: CONFIRMED STATIC / AMPLIFICATION OBSERVED IN RUNTIME

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

`STATUS`: LIKELY

`IMPACT`: high

`PROBABILITY`: medium/high com defaults versionados

`AFFECTED AREA`: Celery Beat

`DESCRIPTION`: beat generate-file desabilita dispatch/heartbeat, mas defaults ainda habilitam reconcile de canal e pos-process FileApp. Dois beats podem publicar as mesmas rotinas.

`MITIGATION`: locks/cooldowns parciais.

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

`DESCRIPTION`: `send_whatsapp_template` compartilha o caminho de `send_whatsapp_interactive` e persiste `blocked_send_whatsapp_interactive` como sucesso. Esse motivo nao pertence a `BLOCKING_RUNNING_STOP_REASONS`, portanto o dispatcher nao aplica a transicao defensiva para `state=1`. Com o claim nao duravel de R1, a sessao pode permanecer `state=0`, ser selecionada a cada scan e retornar imediatamente o mesmo bloqueio, sem reenviar a mensagem e sem produzir alarme.

`RUNTIME EVIDENCE`: em 2026-08-24 15:28 BRT, o flow `4d81d73b-dfee-43b8-9c82-d3c52207941f` tinha sete sessoes GenericApp `state=0` bloqueadas no card de WhatsApp e 4.389.386 metricas de executor `success/blocked_send_whatsapp_interactive`. O flow nao possuia alarmes. As execucoes continuavam aproximadamente a cada dois segundos.

`MITIGATION`: ate uma correcao aprovada, preservar evidencias e isolar as sessoes/dispatcher somente por procedimento operacional controlado. Nao usar contagem de alarmes como unico detector.

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
