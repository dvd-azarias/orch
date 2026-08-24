# Riscos Conhecidos

Baseline estatica de 2026-08-24. Nenhum destes riscos foi corrigido durante o onboarding.

## R1 — Claims do dispatcher sem commit externo

`STATUS`: CONFIRMED STATIC

`IMPACT`: high

`PROBABILITY`: high quando o dispatcher periodico e usado

`AFFECTED AREA`: Celery workflow / PostgreSQL

`DESCRIPTION`: a task lista workspaces, abre transacao implicita, faz claim em savepoint e publica task, mas fecha a sessao sem commit explicito do outer transaction. Claims e metricas podem ser revertidos enquanto o enqueue permanece.

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
