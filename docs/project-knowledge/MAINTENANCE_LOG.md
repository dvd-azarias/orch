# Maintenance Log

## 2026-08-28 — Billing batch persistente `service-orch`

### REQUEST / CLASSIFICATION

Substituir o publisher unitario legado por event store + snapshots agregados, retry persistente, reconciliacao e reprocessamento auditavel. `ALPHA_FIX_OPTIONAL`, risco alto por banco, Celery, concorrencia, idempotencia e RabbitMQ; novo mecanismo protegido por flag default `false`.

### CHANGE

- Legado `ORCH_BILLING_SNAPSHOT_ENABLED` preservado e desligado por default; configuracao rejeita dual enablement com `ORCH_BILLING_ENABLED`.
- Migration `0022` cria eventos, snapshots, solicitacoes de reprocessamento e indice temporal de `orch_sessions`, sem alterar/remover a tabela `0020`.
- Sessao nova, inclusive pelo componente `create_contact`, registra evento fail-open; reconciliador usa `orch_sessions` e chave idempotente por workspace/sessao/periodo/metrica.
- Agregador cria batches maximos de 200 sob `FOR UPDATE SKIP LOCKED` e chama publicacao no mesmo flush; publisher usa payload persistido, `mandatory`, mensagem persistente e confirm.
- Retry indefinido, backoff/teto/jitter, leases com `claim_token`, bloqueio estrutural e reprocessamento idempotente foram adicionados.
- Reprocessamento mensal usa chunks persistentes de 1000, cursor retomavel e reserva de enqueue para nao inflar a fila quando workers estiverem indisponiveis.
- App/fila/worker/Beat dedicados e rotas autenticadas de reprocess/status foram adicionados; nenhum template foi instalado.

### VALIDATION

- Regressao focada final: 157 testes passaram.
- Integracao PostgreSQL fora da sandbox: 2 testes passaram; 450 sessoes em tabelas temporarias viraram 450 eventos e snapshots `200 + 200 + 50`, e a migration exata executou em schema descartavel dentro de transacao revertida.
- Suite completa: 415 passaram e 27 falharam, exatamente nas duas familias do baseline (26 chamadas com assinatura antiga de `trigger_orch` e 1 invalidacao de prepared statement asyncpg em tabela temporaria); nenhuma falha nova de billing.
- `compileall` e `git diff --check` passaram. Revisao independente concluiu `GO` para o patch de codigo.
- Stack local homologada: API, tres workers e dois Beats `up`; smoke real nos dois flows retornou `202 accepted` para as sessoes `7201` e `7202`. Billing permaneceu desligado e nenhuma migration foi aplicada.
- Broker/consumer alvo, concorrencia real multi-transacao, lock do indice em workspace volumoso e E2E do billing apos migration permanecem `UNKNOWN`; nenhuma producao foi acessada.

### ROLLBACK

`ORCH_BILLING_ENABLED=false`, restart dos processos afetados e preservacao das tabelas. Nao reativar o publisher legado sem decisao operacional explicita.

## 2026-08-27 — Card isolado `switch_bot_flow`

### REQUEST

Criar um card separado de `run_flow` que transforme a sessao ORCH em hub WhatsApp, repassando ao BOT o payload Meta original desde o primeiro evento ate o callback terminal.

### TASK TYPE / CLASSIFICATION

Feature operacional isolada / `ALPHA_FIX_OPTIONAL`; blast radius contido por card, feature flag e fila dedicada. `run_flow` nao foi alterado.

### CHANGE

- M2 reconhece `switch_bot_flow`, persiste estado bloqueante e resolve `success`/`exception_*` somente no terminal.
- Worker dedicado consulta e cacheia `runner_token`, envia somente mensagens de usuario ao Runner v5 e descarta status WhatsApp do relay.
- O body do primeiro e dos eventos seguintes preserva o mesmo conteudo JSON recebido da Meta, sem envelope sintetico.
- `target_session_id` e metadados da revisao ficam persistidos; callback idempotente retoma o M2 e o primeiro terminal vence.
- Filas isoladas foram adicionadas aos profiles DEV/launchd/prod e aos manifests versionados.

### VALIDATION

- Regressao direcionada final: 135 testes passaram; `compileall`, `git diff --check`, `bash -n` e `plutil -lint` passaram.
- Suite completa: 354 passaram e 26 falharam em baseline legado, primeiro por `trigger_orch(flow_uuid=...)` desatualizado.
- Consulta real ao Target Core resolveu um `runner_token` de 64 caracteres sem expor o segredo.
- Stack local completa subiu com `orch_switch_bot_flow_f5_local`; worker registrou a task e o smoke encadeado dos dois flows retornou aceite.
- POST real ao Runner, resposta do BOT via Meta e callback terminal permanecem pendentes de canario controlado para evitar mensagem externa acidental.

### CANARIO DE PRODUCAO — 2026-08-27

- O template anterior ao card chegou ao contato, confirmando ausencia de regressao no caminho existente.
- O POST real ao Runner foi aceito e a engine gerou resposta, mas cada payload criou uma sessao nova porque o token usava `session_key=chat.id` e a entrada foi classificada como provider `webhook`.
- Todos os dispatches do BOT terminaram em `missing_integration`; nenhuma resposta chegou a Meta.
- O ORCH detectou a troca de `session_id`, persistiu `switch_bot_flow_runner_session_mismatch` e percorreu o branch de excecao como projetado.
- Auditoria read-only posterior confirmou que o provider e definido pela URL. O ORCH usou `/webhook/session`, enquanto `/whatsapp/session` ja interpreta o envelope Meta, mantem a identidade pelo `wa_id` e despacha pela API WhatsApp generica configurada nos hosts Target Core; nao foi identificada necessidade de alterar o codigo do Target Core antes do proximo canario.
- Diagnostico completo em `INCIDENT_HISTORY.md` e risco `R28` em `KNOWN_RISKS.md`. Nenhum ajuste funcional ou de producao foi feito durante a investigacao.
- Correcao preparada no ORCH: troca cirurgica do provider da URL para `whatsapp`, sem alterar payload, retry, correlacao local ou contratos dos demais cards. Deploy e novo canario permanecem pendentes.
- O segundo canario confirmou o ciclo completo de mensagens e o contrato terminal real do BOT: `POST /v1/orch/{alias}` com `entity`, `session.id`, `variables` e `disposition`. O `session.id` `9706f438-80be-47b7-a0e4-9923b1c489f0` coincidiu exatamente com o `target_session_id` da sessao ORCH `7105`.
- Antes da correcao, esse terminal entrou como `GenericApp` e criou a sessao fantasma `7106`, enquanto `7105` permaneceu bloqueada. A correcao Alpha intercepta apenas o caminho por alias, exige correlacao exata com um handoff do mesmo flow, reaproveita o callback idempotente e preserva o trigger legado quando nao houver correspondencia.
- Regressao direcionada final da correcao terminal: 130 testes passaram; lint/compile e `git diff --check` passaram. A stack local completa ficou `up`, o smoke encadeado dos dois flows passou e o replay HTTP do payload real retornou `persistence=switch_bot_flow_callback`, `session_created=false` e `session_id=7105`. O worker levou a sessao `7105` a `state=3` com `ended_at`; o replay seguinte retornou `idempotent=true` e `session_state=3`, sem criar nova sessao. A stack local foi encerrada sem processos residuais.

### ROLLBACK

Definir `SWITCH_BOT_FLOW_ENABLED=false` e reiniciar API/worker. Flows sem o novo card e o comportamento de `run_flow` permanecem inalterados.

## 2026-08-26 — Recibo idempotente para entrega imediata FileApp

### REQUEST

Eliminar a dependência operacional do rescue de 10 minutos para eventos S3/FileApp do fluxo crítico, mantendo o rescue como rede de segurança.

### TASK TYPE / CLASSIFICATION

Incident remediation / `ALPHA_FIX_REQUIRED` (high risk: FileApp, Celery e migration multi-workspace).

### CHANGE

- A migration `0021_create_fileapp_ingest_receipts` cria um recibo por `(flow_uuid, file_id)` no schema do workspace.
- O trigger Tipo 1 reivindica e confirma o recibo antes de publicar no Celery; replays em estados ativos/terminais retornam `202` idempotente sem novo enqueue.
- O worker Tipo 1 carrega o `receipt_id` e registra `processing`, `completed` ou `failed` em modo best-effort.
- O rescue reivindica o mesmo recibo antes de publicar; um recibo originado pelo webhook impede reingestão duplicada.
- O recibo agora expõe explicitamente `should_enqueue`: uma primeira recepção e a retomada de `failed`/`enqueue_failed` publicam uma task; estados ativos ou concluídos permanecem replays idempotentes.
- A correção residual remove o receipt de entrega Target Core da decisão de “ingestão existente”, recupera `accepted` stale após 60 segundos, registra `task_id` no enqueue do rescue e usa o batch como limite de ações, sem starvation por skips.
- A correção da corrida do step 5 aceita `INGESTING`/`PROCESSED` como auto-ingestão já iniciada/concluída pelo Target Core e não publica um segundo import nesses estados; estados desconhecidos continuam fail-closed.

### VALIDATION

- `pytest -q tests/test_fileapp_ingest_tasks.py tests/test_fileapp_entrada_rescue_task.py tests/test_migration_service.py`: 22 passed.
- Após a correção de retomada: `pytest -q tests/test_fileapp_ingest_receipt_api.py tests/test_fileapp_entrada_rescue_task.py tests/test_fileapp_ingest_tasks.py`: 22 passed; `compileall` dos módulos alterados passou.
- Runtime de produção após o merge `613ac46`: API `ready=true`; cinco workers FileApp ativos; 15 arquivos reenviados pela rota oficial retornaram `202 queued`, concluíram com receipts `completed` e foram encontrados em `monitoramento/upload/processados`; zero em `falha`, zero ausentes e zero restantes em `monitoramento/upload`.
- O runtime também confirmou risco residual: `arquivos_s3_events` pode fazer o rescue marcar `done` sem `source_list`, mantendo o arquivo físico e causando starvation com batch `2`; registrado em `KNOWN_RISKS.md` R25.
- Correção residual: 28 testes passaram; `compileall` passou; claim validado no PostgreSQL real com primeira aceitação, replay fresco bloqueado e reclaim stale permitido, tudo revertido por rollback.
- Stack local completa subiu com filas `f5_local`, API/workers ficaram prontos e o smoke canônico dos dois flows passou. A stack foi encerrada e não restaram Uvicorn/Celery locais do repositório.
- Validação de migration em DB real, subida da stack e E2E cruzado com Target Core permanecem pendentes.
- Testes focados da corrida do step 5: `12 passed`; regressão FileApp ampliada: `73 passed`, cobrindo `READY_TO_INGEST`, `INGESTING`, `PROCESSED` e rejeição de estado regressivo; `compileall` passou. A stack local completa ficou pronta e o smoke canônico dos dois flows passou; nenhum processo local permaneceu ativo depois da validação.

### ROLLBACK

Desabilitar a migration/código desta entrega antes do rollout; após a migration ser aplicada, preservar a tabela e reverter apenas os consumidores para manter os recibos auditáveis.

## 2026-08-24 — Primeiro onboarding formal do Project Steward

### REQUEST

Executar integralmente a secao `PRIMEIRA EXECUCAO`, sem alterar codigo funcional, usando arqueologia paralela, revisao adversarial e memoria persistente.

### TASK TYPE

Investigation / documentation.

### CHANGE

- Criado `PROJECT_BRAIN.md`.
- Criada baseline em `docs/project-knowledge/` para arquitetura, componentes, fluxos, dados, dependencias, integracoes, configuracao, operacao, quirks, riscos e divida tecnica.
- Nenhum arquivo funcional, migration, fila, dependencia ou configuracao de producao foi alterado.

### EVIDENCE

- `AGENTS.md`, `PROJECT_STEWARD.md` e `README.md` lidos integralmente.
- Runbooks, scripts, units, SQL, rotas, services, repositories, tasks, testes e historico Git inspecionados.
- Cinco arqueologias independentes: API/workflow, Celery, dados/migrations, FileApp e runtime/integracoes.
- Tres revisoes adversariais concluidas sobre os achados de maior risco; nenhuma das conclusoes centrais foi refutada, e qualificacoes foram incorporadas.

### VALIDATION

- `pytest --collect-only -q`: 295 casos.
- `pytest -q` fora da sandbox: 270 passaram, 25 falharam em 192.08s.
- As 25 falhas observadas param primeiro em `trigger_orch(flow_uuid=...)`, assinatura anterior a `alias_or_flow_uuid`; revisao estatica indica que alguns casos podem ter expectativas semanticas adicionais desatualizadas.
- Runtime local completo nao foi iniciado; health/E2E permanecem `UNKNOWN`.

### RISK

Documentacao: low. Achados operacionais registrados em `KNOWN_RISKS.md`, sem correcao nesta etapa.

### ROLLBACK

Remover somente `PROJECT_BRAIN.md` e `docs/project-knowledge/` criados neste onboarding.

### NOTES

A baseline e majoritariamente estatica. Estado de producao, integracoes e persistencia FileApp tipo 1 precisam de verificacao observacional/E2E futura.

## 2026-08-24 — Analise do flow ORQUESTRADOR

### REQUEST

Determinar se o flow `0e378237-4a61-4d5f-89f3-b07b594df38f` possui gaps.

### TASK TYPE

Incident / production analysis, somente leitura.

### EVIDENCE

- Flow localizado no workspace `91c85c54-cd88-4aed-88e0-7eb720674f5d`, ativo em cadastro, mas `draft` e sem revisao publicada.
- Grafo e revisao draft extraidos do PostgreSQL; implementacao M1/M2, dispatcher, executor e FileApp confrontada com a definicao.
- Tres sessoes encontradas no cursor inicial, quatro eventos WhatsApp pendentes e 1.136.779 alarmes de execucao ate 2026-08-24 11:35 BRT.
- Quatro workers workflow observados consumindo as filas `orch_dispatch`, `orch_execute` e `orch_heartbeat` em dois hosts.
- Revisao adversarial independente confirmou os gaps centrais e qualificou como hipotese a atribuicao de versoes distintas aos workers.

### FINDINGS

- Condition sem branch `false/exception`, embora declare `has_exception_branch=true`.
- `api_call` de EMAIL e SMS sem URL; EMAIL sem branches `error/exception`.
- Flow filho de pre-vendas tambem esta somente em draft.
- Mapping template existente, mas `QuantidadeParcelas` e `Cpf` apontam simultaneamente para `CONTACT_IDENTIFIER`.
- Erros permanentes sao amplificados por reenfileiramento continuo; nenhuma intervencao foi executada.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED`, risco critical. Detalhes em `docs/project-knowledge/INCIDENT_HISTORY.md`.

## 2026-08-24 — Analise do flow Demo WhatsApp Outbound

### REQUEST

Avaliar o flow `4d81d73b-dfee-43b8-9c82-d3c52207941f` no workspace `ba7eb0ec-e565-447c-8c11-8f870cf72a60`, com atencao a uma sessao relacionada ao telefone informado pelo operador.

### TASK TYPE

Incident / production analysis, somente leitura.

### EVIDENCE

- Revisao publicada v13 extraida e grafo confrontado com runtime, metricas, sessoes e eventos.
- A sessao `6927` percorreu `confirmar -> set_variables -> api_call 200 -> live` e terminou em `component_not_supported:live`; nao havia sessao nao terminal para o telefone consultado na fotografia de 15:27 BRT.
- Sete sessoes GenericApp `state=0` acumulavam 4.389.386 execucoes `blocked_send_whatsapp_interactive` sem alarmes ate 15:28 BRT.
- Branch atual, `main` e commit isolado `bd461a5` comparados sem checkout ou alteracao de Git.
- Revisao adversarial independente identificou claim sem commit e omissao de `blocked_send_whatsapp_interactive` na transicao defensiva como defeitos distintos.

### FINDINGS

- Retry storm silencioso confirmado e ainda ativo quando observado.
- Handoff humano nao ocorreu para a resposta do operador.
- O suporte `live` isolado no historico nao constitui correcao pronta, pois nao executa side effect externo.
- Semantica de `encerrar`, aliases de eventos e convergencia dos outcomes da API precisam de confirmacao funcional.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED`, risco critical. Detalhes em `docs/project-knowledge/INCIDENT_HISTORY.md` e `docs/project-knowledge/KNOWN_RISKS.md`.

## 2026-08-24 — Contencao e correcao do retry storm por branch ausente

### REQUEST

Terminalizar as sessoes presas do flow `0e378237-4a61-4d5f-89f3-b07b594df38f` e fazer `condition` sem branch compativel encerrar a sessao como falha.

### TASK TYPE

Production containment / `ALPHA_FIX_REQUIRED`.

### CONTAINMENT

- Fotografia imediatamente anterior: sessoes `256` e `257` em `state=0`, cursor inicial e `ended_at=NULL`; 1.153.946 alarmes, ainda crescendo.
- Update transacional e guardado terminalizou somente esses IDs com marcador de falha operacional.
- Houve drenagem residual de 28 alarmes de tasks anteriores; a primeira contagem estabilizou em 1.153.974.
- A stack local `f5_local` usada na validacao permaneceu ativa e revelou que `CELERY_DISPATCH_WORKSPACE_UUID` nao escopa o reconciliador. Sem `CELERY_RECONCILE_PENDING_EVENTS_WORKSPACE_UUID`, ele encontrou a sessao WhatsApp `263`, `state=2`, em outro workspace e gerou 51 alarmes adicionais por `api_call_missing_url`.
- A sessao `263` foi terminalizada com guard exato as 16:50 BRT e a stack local foi parada pelo script oficial. O ultimo alarme antecedeu a terminalizacao; a contagem final permaneceu em 1.154.025 apos nova janela de 60 segundos.
- Nenhuma sessao elegivel permaneceu no flow.

### CHANGE

- `condition_branch_not_mapped` agora terminaliza com `state=3`, `ended_at`, cursor nulo e runtime de diagnostico.
- Async Celery registra alarme/metricas como erro e commita a terminalizacao.
- Tasks atrasadas param somente quando encontram estado terminal acompanhado de `workflow_v2.terminal_failure`.
- Fluxos normais que chegam a M2 em `state=3`, como callbacks finais de canal, permanecem inalterados.

### VALIDATION

- `git diff --check`: passou.
- `compileall` dos arquivos alterados: passou.
- Testes focados sem DB: 87 passaram.
- Primeira revisao adversarial encontrou guard terminal amplo demais; correcao aplicada.
- Segunda revisao adversarial: nenhum achado acionavel.
- Smoke isolado, sem beat/dispatcher: API local retornou `202` para os fluxos canonicos A e B; as duas tasks foram recebidas e concluidas com sucesso pelo worker dedicado.
- A tentativa anterior de stack completa nao conta como validacao segura de isolamento: o dispatcher estava escopado, mas o reconciliador nao. O incidente e o risco foram documentados.
- Teste DB nao foi repetido porque o `.env` aponta para workspace real; a suite DB existente continua com casos stale que usam `trigger_orch(flow_uuid=...)`.

### ROLLBACK

- Codigo: reverter os arquivos funcionais desta manutencao.
- Sessoes contidas: restauracao exige decisao operacional explicita; nao reabrir enquanto o flow permanecer invalido.

### DEPLOYMENT

Nao executado nesta etapa. A contenção de producao esta ativa; a correcao protege novas sessoes somente apos deploy/restart validado.

## 2026-08-24 — Investigacao de `linked_actuator` ausente no flow de Dialer

### REQUEST

Investigar por que o membro da lista `dc7dc1c1-2c98-42e9-a788-5d186f458daa` permanecia sem `linked_actuator=dialer` no flow `3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17`.

### TASK TYPE

Production diagnosis read-only / `ALPHA_FIX_REQUIRED`.

### EVIDENCE

- Branch `investigate/dialer-linked-actuator` criado de `origin/main` no merge `1dc8494`.
- Flow ativo, revisao publicada v5 e primeiro card `send_with_dialer` confirmados.
- Sessao ativa `6937` executou o card e persistiu `blocked_send_with_dialer`.
- Payload de `6928/6937`: lista `dc7dc1c1-2c98-42e9-a788-5d186f458daa`, mailing `1115`.
- Runtime de ambas: assignment para membro `10687`, lista `b5521cb2-09a9-4391-8ab5-fea25924e820`, mailing `1114`.
- O membro esperado `10655` permaneceu nulo; o membro `10687` recebeu `dialer`.
- Blast radius conservador: 38 identificadores duplicados ativos, 113 linhas e 26 sessoes divergentes em tres flows.

### FINDING

O ORCH nao deixou de setar o actuator. Ele o setou na linha errada porque o seletor ignora a lista/mailing do payload e escolhe o membro ativo mais novo para o mesmo `contact_identifier`. O mesmo padrao existe no roteamento WhatsApp e no carregamento do contexto de contato.

### REVIEW

Revisao adversarial independente confirmou a causa e recomendou resolver o membro por identidade contextual, com fallback legado apenas na ausencia completa de seletores.

### CHANGE

Na etapa inicial, nenhum codigo funcional ou dado de producao foi alterado. Apos aprovacao, foi implementada correcao protegida por feature flag default-off:

- escopo imutavel extraido de `input_payload`;
- resolucao unica e reutilizada por contexto, Dialer e WhatsApp;
- seletores combinados validados cruzadamente, sem fallback em conflito;
- queries especializadas com tipos nativos `uuid`/`bigint`;
- lock/revalidacao antes do update e falha terminal em corrida;
- alarmes equivalentes nos caminhos Celery e inline.

### VALIDATION

- tipos reais confirmados read-only no workspace: `contact_list_id=uuid`, `mailing_id=bigint`;
- `python -m py_compile`: passou nos arquivos alterados;
- `git diff --check`: passou;
- testes focados de M2, repositorio e tasks: 105 passaram;
- duas revisoes adversariais independentes executadas; os achados bloqueantes foram incorporados;
- runtime isolado e smoke E2E ainda pendentes.

Validacao posterior:

- consulta read-only no caso real confirmou: fallback legado -> membro `10687`; escopo exato/lista+mailing -> `10655`; conflito cruzado -> nenhum membro;
- teste PostgreSQL com tabelas temporarias executou resolucao, `FOR UPDATE` e updates Dialer/WhatsApp sem tocar tabelas permanentes; 1 passou;
- a primeira execucao desse teste encontrou parametro asyncpg ambiguo nos atuadores; a query foi especializada e a repeticao passou;
- tentativa de stack completa invalidada por processos stale nao controlados pelo script; todos os processos locais foram contidos, nenhum consumer `f5_local` permaneceu no broker e R21 foi registrado;
- nenhum smoke HTTP foi enviado. O E2E de stack permanece pendente.
- formatos reais agregados no workspace: 59 payloads com `contact_list_member_id` numerico, 437 com `contact_list_id` UUID e 378 com `mailing_id` numerico; nenhum UUID foi observado nos dois campos `BIGINT`;
- o runbook Supplier exemplificava incorretamente member/mailing como UUID e foi corrigido para refletir o schema e o runtime reais.
- revisao adversarial final do bundle funcional: `GO`, sem achados criticos, altos ou bloqueantes; risco residual limitado a concorrencia real entre transacoes nao simulada;
- follow-up de seguranca: rotacionar a credencial SFTP exibida no terminal por uma consulta de auditoria excessivamente ampla; nenhum segredo foi copiado para arquivos versionados.

### ROLLOUT / ROLLBACK

Implantar inicialmente com `WORKFLOW_CONTEXTUAL_MEMBER_ROUTING_ENABLED=false`; validar com filas e workspace isolados usando `true`; somente depois habilitar no ambiente alvo e reiniciar. Rollback operacional: flag `false` + restart. Reparacao historica permanece separada e nao automatizada.

## 2026-08-24 — Pente-fino pos-migracao dos workers para `10.1.20.237`

### REQUEST

Auditar o runtime depois de varias tasks processadas no novo host e identificar GAPs diretamente ligados ao cutover.

### TASK TYPE

Production audit read-only / `ALPHA_FIX_REQUIRED` para os achados criticos e altos.

### CONFIRMED

- API, tres beats e quinze workers ORCH ativos/enabled no `10.1.20.237`, sem restart de unit ou unit falha; API do `10.1.20.136` permaneceu ativa por restricao do proxy e seus workers/beats ORCH permaneceram desabilitados.
- As oito filas ORCH consultadas passivamente tinham cinco consumers e zero mensagens prontas; `active/reserved/scheduled` nao mostrou backlog de workflow.
- FileApp e generate-file concluiram todas as tasks observadas na primeira janela, sem task failure marker.
- A correcao contextual esta efetiva: 46 sessoes novas explicitas produziram 46 assignments e zero divergencias de membro, lista, mailing ou atuador. A sessao `6941` do flow `3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17` resolveu `10655` e deixou `linked_actuator=dialer`.
- Tres beats publicavam schedules sobrepostos; os arquivos de schedule eram distintos, refutando disputa do arquivo local.
- O loop `blocked_send_whatsapp_interactive` permanecia ativo em tres workspaces. A janela desde 19:22 BRT produziu mais de 213 mil execucoes e 428 mil metricas; o ORCH escreveu aproximadamente 1,27 milhao de linhas/234 MB no journal.
- Os doze schemas com metricas somavam estimativa de 199,4 milhoes de linhas e mais de 90 GB.
- O host tinha 152 processos Celery, cerca de 12,9 GB RSS e 187% de CPU agregada no snapshot; havia capacidade de RAM/disco, mas churn elevado de processos/logs.
- SIGTERM de child process expos `UnboundLocalError` por `stopped_reason` nao inicializado no wrapper da task.
- `/health/celery` aceita qualquer worker do vhost e `/health/ready` nao inclui Celery; ambos sao insuficientes isoladamente para validar o cutover.

### ADVERSARIAL REVIEW

Dois revisores independentes confirmaram: severidade critica para o loop WhatsApp; alta para o wrapper de task, falso positivo de health e readiness sem Celery; critica para publishers duplicados segundo o revisor operacional. A classificacao operacional final manteve a duplicacao como `high`, pois locks/cooldowns limitam parte dos efeitos e nao houve backlog no snapshot.

### UNKNOWN

- Motivo exato dos SIGTERM nos childs.
- Efeito funcional atual de Target Core, Files API, LLM e SFTP fora das tasks observadas.
- Retencao efetiva do journal sob o volume atual e capacidade livre do servidor PostgreSQL.

### CHANGE

Somente documentacao de conhecimento. Nenhum codigo funcional, unit, processo, fila, sessao ou dado de producao foi alterado.

## 2026-08-24 — Correcao do loop `blocked_send_whatsapp_interactive`

### REQUEST

Interromper a reexecucao massiva de sessoes WhatsApp bloqueadas sem alterar o contrato de espera por callback.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED` — risco critico de amplificacao operacional e crescimento de metricas/logs.

### ROOT CAUSE

O executor M2 retorna `blocked_send_whatsapp_interactive` como bloqueio valido para template e interativo, mas o dispatcher nao incluia esse motivo em `BLOCKING_RUNNING_STOP_REASONS`. A sessao podia voltar a `state=0` e ser reclamada novamente pelo scan periodico; cada ciclo criava uma nova task Celery com `Retries=0`, mascarando a tempestade como varias execucoes bem-sucedidas independentes.

### CHANGE

- Inclusao cirurgica de `blocked_send_whatsapp_interactive` no conjunto bloqueante do dispatcher.
- Regressao unitaria exige `state=1`, `only_if_not_finished=True` e ausencia de finalizacao da sessao.
- Nenhuma sessao, fila, unit ou dado de producao foi alterado nesta etapa.

### VALIDATION

- Teste novo falhou antes da mudanca funcional, comprovando a regressao.
- Suite focada: 103 testes passaram em `tests/test_workflow_dispatcher_service.py`, `tests/test_workflow_m2_whatsapp_interactive.py`, `tests/test_workflow_m2_service.py` e `tests/test_workflow_tasks.py`.
- Revisao adversarial independente: `GO`, sem achados bloqueantes; confirmou exclusao dos scans por `state=1` e retomada direta por callback/reconciliador.
- Limitacao conhecida: falta teste integrado PostgreSQL do ciclo completo `bloqueio -> callback/evento -> retomada`.
- Deploy e validacao no host `10.1.20.237` permanecem pendentes.

## 2026-08-24 — Webhook terminal do `finish_flow`

### REQUEST

Suportar os novos campos do card `finish_flow` da revisao publicada do flow `3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17`, enviando os dados da sessao ao webhook e incluindo o evento de telefonia quando o fluxo usa Dialer.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED` — o campo ja publicado nao possuia efeito no runtime.

### CONFIRMED CONTRACT

- Existe apenas um evento de telefonia por sessao; `cdr` e um objeto, nunca lista ou acumulador.
- O payload Dialer e copiado para a sessao dona somente quando a revisao selecionada, consultada no recebimento do evento, possui `finish_flow.parameters.webhook` nao vazio.
- O `finish_flow` envia uma vez a linha completa da sessao com `result` e `cdr`; o CDR nao e duplicado em `runtime_variables`. Resposta `2xx` remove o CDR, enquanto falha o preserva.

### CHANGE

- Persistencia JSONB cirurgica no `runtime_variables.cdr`, sem migration.
- Envio direto pelo mecanismo HTTP ja existente, sem outbox, scanner, beat, fila ou worker adicional.
- Registro do resultado em `runtime_variables.finish_flow_webhook` para diagnostico.

### VALIDATION

- Regressao ampliada: 129 testes passaram; `compileall` e `git diff --check` passaram.
- Teste de execucao completa confirma que o branch `finish_flow` persiste e rele o snapshot terminal antes do dispatch, e que `2xx` remove o unico CDR na persistencia seguinte.
- PostgreSQL real validado com tabela temporaria e rollback: JSONB permanece objeto e uma nova escrita substitui, sem acumular; o snapshot completo e retornado por `to_jsonb`.
- Smoke HTTP real em destino loopback observou um unico `POST`, um unico campo `cdr` e limpeza em memoria apos `204`.
- As revisoes adversariais encontraram dependencia temporal da flag, snapshot parcial, CDR duplicado e leitura anterior a terminalizacao; os quatro achados foram removidos antes do fechamento.
- A revisao final confirmou esses quatro pontos e levantou somente o risco preexistente `R7`: publicacao entre recebimento e execucao pode trocar a revisao relida pelo M2. Pinagem global de revisao foi mantida fora do escopo para nao ampliar o blast radius desta mudanca.
- O POST em destino real foi observado em 2026-08-25; a entrega funcionou e revelou o payload inflado, o reenvio e o ledger pendente registrados na manutencao seguinte.

## 2026-08-25 — Higienizacao e unicidade do webhook `finish_flow`

### REQUEST

Remover repeticoes do payload observado em producao e garantir a paridade entre uma sessao de voz, seu unico CDR e um unico webhook terminal confirmado.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED` — o primeiro teste real enviou estado interno volumoso, repetiu o POST para a mesma sessao e deixou eventos Dialer pendentes sendo reconciliados continuamente.

### RUNTIME EVIDENCE

- A sessao `6945`, workspace `ba7eb0ec-e565-447c-8c11-8f870cf72a60`, flow `3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17`, entregou o webhook com `runtime_variables` inteiro e varias copias do mesmo callback.
- Dois eventos Dialer distintos (`GW02-1787649908.293874` e `GW01-1787649921.293865`) chegaram para a mesma sessao, contrariando o contrato funcional de uma chamada/desfecho por sessao.
- O runtime anterior enviou dois webhooks `200` e deixou os dois registros do ledger com `processed_at=NULL`; o reconciliador continuou executando a sessao terminal.

### CHANGE

- O body passa a conter somente campos persistidos da sessao fora de `runtime_variables`, mais `result` e o unico `cdr` persistido.
- O primeiro resultado `2xx` persistido impede novos POSTs da mesma sessao e remove o CDR.
- O sucesso baixa todos os eventos Dialer excedentes ainda pendentes. Evento tardio apos sucesso e preservado no ledger, mas marcado processado e impedido de recriar o CDR.
- Nenhuma migration, fila, beat, worker ou armazenamento novo foi criado.

### VALIDATION

- Suite focada e ampliada: 132 testes passaram, incluindo payload sem runtime interno, paridade do CDR persistido, supressao de reenvio, limpeza de backlog e tratamento de evento tardio.
- `compileall` e `git diff --check` passaram.
- Smoke HTTP loopback observou exatamente um POST, sem `runtime_variables`, com um CDR; a segunda execucao foi suprimida.
- PostgreSQL real com tabelas temporarias e rollback confirmou CDR unico, bloqueio de ressurgimento apos sucesso e baixa de dois eventos Dialer sem afetar evento WhatsApp.
- A stack local iniciou API, tres workers e dois beats; API e tasks responderam. O runbook abortou e limpou os processos porque `scripts/dev_phase_stack.sh` passa simultaneamente `--hostname` e `-n`: o nome efetivo nao casa com o regex de readiness. O gap e preexistente e ficou fora do patch funcional.
- Duas revisoes adversariais inicialmente deram `NO-GO`; a implementacao foi simplificada para remover fallback de CDR divergente e claim antecipado no `send_with_dialer`. O risco residual best-effort entre `2xx` e commit permanece documentado em R24.
- Deploy e validacao E2E no host `10.1.20.237` permanecem pendentes.

## 2026-08-25 — Paridade deterministica entre sessao, contato e CDR

### REQUEST

Corrigir o teste de producao em que o primeiro webhook higienizado saiu sem CDR e a execucao seguinte nao produziu POST, preservando o contrato de uma sessao de voz, um desfecho e um CDR cru.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED` — perda de dado terminal e supressao silenciosa de webhook em producao.

### ROOT CAUSE

- O executor carregava um snapshot terminal, mas `_dispatch_finish_flow_webhook` buscava `cdr` em outro dicionario de runtime potencialmente antigo.
- `replace_session_workflow_state` substituia o JSONB completo antes do dispatch, ampliando a janela de perda da escrita cirurgica feita no ingresso.
- O sucesso era por sessao e o codigo baixava todos os eventos Dialer pendentes; o fallback temporal ainda podia reabrir uma sessao ja confirmada.
- O payload achatava a sessao e removia o unico lugar de onde o contato normalizado era obtido.

### CHANGE

- `orch_channel_events` passa a ser a fonte autoritativa do CDR cru no `finish_flow`; a copia residual em runtime nunca e usada como fallback de envio.
- O body e `session` (dados persistidos, `result`, `contact`) mais `cdr`; estado interno nao sai.
- Fluxo Dialer sem CDR adia o POST; `2xx` processa somente o evento selecionado e limpa a copia transitoria.
- Evento tardio e marcado individualmente e o fallback recente exclui sessao com webhook ja confirmado.
- Sem migration ou infraestrutura nova.

### VALIDATION

- Suite focada: 31 testes passaram.
- Regressao relevante de trigger, Dialer, workflow e repositorios: 153 testes passaram.
- Teste adversarial reproduz runtime local sem CDR e confirma que o payload cru vem do ledger, com o evento exato marcado apos `2xx`.
- `py_compile` e `git diff --check` passaram.
- A primeira revisao adversarial recusou a inferencia por grafo e o lock durante HTTP; ambos foram removidos. A segunda recusou fallback pelo runtime; ele tambem foi removido, mantendo o ledger como unica fonte externa do CDR.
- A revisao adversarial final deu `GO`: confirmou origem exclusiva no ledger, baixa do evento exato, bloqueio de reabertura apos `2xx` e ausencia de `runtime_variables` no contrato externo.
- Suites integradas antigas ainda falham antes do codigo alterado pela assinatura removida `trigger_orch(flow_uuid=...)`, gap ja documentado na baseline.
- Deploy e observacao do POST real permanecem pendentes.

## 2026-08-25 — Webhook por tentativa Dialer da mesma sessao

### REQUEST

Preservar a sessao unica de um contato no `send_with_dialer`, mas publicar um webhook para cada CDR de tentativa sequencial enquanto o Dialer esgota suas tentativas.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED` — a sessao `6949` recebeu dois CDRs distintos, mas o segundo POST foi suprimido pelo `2xx` do primeiro, apesar de a segunda chamada pertencer a uma nova tentativa valida da mesma sessao.

### ROOT CAUSE

- O bloqueio de sucesso era por sessao, quando a semantica Dialer e por tentativa/CDR.
- O fallback de hangup recusava retomar uma sessao com webhook confirmado e a persistencia do CDR tambem recusava gravar novo CDR apos esse sucesso.

### CHANGE

- Hangup Dialer pode retomar explicitamente a sessao recente confirmada para processar uma nova tentativa.
- Cada CDR distinto pode disparar o webhook; o `Idempotency-Key` continua determinado por sessao e evento.
- O `discard_reason` do registro em `orch_channel_events` recebe `finish_flow_webhook_dispatched` apos `2xx`, tornando o ledger a marca duravel que suprime somente o replay do mesmo CDR.
- A identidade e o `uniqueid`/`Linkedid` recebido do Dialer; sem ela, o ORCH usa hash deterministico do payload como fallback, sem migration.
- O body externo e inalterado: `session` limpa mais `cdr` cru, sem `runtime_variables`.

### VALIDATION

- Testes focados e de regressao de workflow, ledger, persistencia, trigger e mapper Dialer: `160 passed`.
- Regressao cobre dois CDRs distintos na mesma sessao gerando dois POSTs e a reentrega de CDR marcado gerando zero POST adicional.
- Revisao adversarial auxiliar foi iniciada, mas o client local do agente perdeu permissao para inicializar antes de produzir veredito; a revisao estatica final foi feita localmente. A garantia permanece *at-least-once* no limite entre `2xx` externo e commit local, risco ja registrado em `KNOWN_RISKS.md`.
- Sem migration, filas, beats ou infraestrutura nova.

## 2026-08-25 — Contencao de replay por marcador de CDR

### INCIDENT

O CDR `GW01-1787659477.294612` da sessao `6950` recebeu `2xx`, mas foi reenviado continuamente pelo reconciliador de eventos pendentes.

### ROOT CAUSE

- O SQL de `mark_channel_event_processed` reutilizava `:discard_reason` em `COALESCE` e em `IS NOT NULL` sem tipagem explicita.
- Com `asyncpg`, PostgreSQL recusou a query com `AmbiguousParameterError`; a transacao foi revertida apos o POST externo e o evento permaneceu pendente.

### CONTAINMENT

- O operador autorizou marcar somente o evento `13908` como `finish_flow_webhook_dispatched` em producao, preservando o CDR distinto `13909` pendente.

### FIX

- Tipar `discard_reason` como `TEXT` em ambos os usos da query para que a marca duravel seja persistida apos `2xx`.
- Validacao local focada: `48 passed`, `py_compile` e `git diff --check`.
- Deploy e verificacao de um CDR novo continuam pendentes.

## 2026-08-31 — Materializacao de HSM no ORCH

### REQUEST

Retirar do Target Core a interpretação de flow/card durante o Contact Supplier e fazer o ORCH definir o HSM do contato em foco.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED`; risco crítico de integração e ordem de rollout.

### ROOT CAUSE

O ORCH persistia apenas ANI/`linked_actuator`. O Supplier carregava a definição publicada e tentava inferir o card HSM por `contact.extra.template_name`; no flow `95c0b826-5834-453f-8a20-f80d328b2e57`, a dica do contato e o template do card divergiam e a seleção retornou `hsm=null` após marcar o contato em execução.

### CHANGE

- Os três cards HSM historicamente aceitos materializam texto, log, payload, template, idioma, card e ANI em `contact_list_members.outbound_hsm`.
- ANI, consumo e HSM usam o mesmo savepoint; falha reverte roteamento e segue branch `exception*` ou terminaliza com `whatsapp_hsm_*`.
- Reentrada da mesma sessão/revisão/card reutiliza o HSM pela chave de idempotência e não incrementa novamente o rate limit.
- O nome em `contact.extra` não escolhe o template; o card efetivamente executado é a autoridade.
- O log estruturado `orch.workflow.m2.whatsapp_hsm_preparation_failed` registra falhas sem payload/PII.

### VALIDATION

- `tests/test_workflow_m2_service.py` + `tests/test_orch_sessions_repository.py`: 114 passaram.
- `tests/test_workflow_m2_whatsapp_interactive.py` fora da sandbox: 4 passaram contra PostgreSQL configurado, incluindo branch `exception` para falha HSM.
- `py_compile` e `git diff --check` passaram.
- Migration Target, stack completa, Supplier real e envio externo permanecem pendentes; nenhum deploy foi executado.
