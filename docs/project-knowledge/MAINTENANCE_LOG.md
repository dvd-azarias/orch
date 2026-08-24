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
