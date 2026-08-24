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
