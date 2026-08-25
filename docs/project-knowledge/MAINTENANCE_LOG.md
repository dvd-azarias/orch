# Maintenance Log

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
- Observacao do POST em destino real e smoke E2E permanecem pendentes.
