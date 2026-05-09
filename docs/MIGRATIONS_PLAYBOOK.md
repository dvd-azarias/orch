# Playbook de Migrations (orch)

Este documento define o **processo obrigatório** para qualquer alteração de estrutura de banco do `orch`.

## Objetivo

Garantir mudanças de schema com segurança em ambiente multi-workspace (`ws_*`), sem impacto na aplicação core e sem tocar em objetos fora do escopo `orch`.

## Regras de Ouro

- **Nunca** alterar tabelas da aplicação core.
- **Nunca** tocar na tabela `alembic_version` existente dos workspaces.
- Controle de versão do `orch` é **somente** em `orch_alembic_version` (por schema `ws_*`).
- Toda mudança estrutural (nova tabela, coluna, índice, constraint) deve virar **arquivo SQL versionado** em `sql/`.
- Mudanças devem ser **idempotentes** (`IF NOT EXISTS`, `DROP CONSTRAINT IF EXISTS` + `ADD`, etc.).
- Não usar SQL destrutivo em produção sem plano explícito (ex.: `DROP TABLE`, `DROP COLUMN`).

## Estrutura atual

- Serviço de migration: `app/services/migration_service.py`
- Lista de migrations aplicadas: constante `MIGRATIONS` em `app/services/migration_service.py`
- Controle por workspace: tabela `ws_<workspace_uuid>.orch_alembic_version`
- CLI oficial:
  - `python -m app.cli migrate-all`
  - `python -m app.cli migrate-workspace <workspace_uuid>`
- Endpoints API:
  - `POST /v1/orch/admin/workspaces/{workspace_uuid}/migrate`
  - `POST /v1/orch/admin/workspaces/migrate-all`

## Fluxo obrigatório para criar/alterar tabelas

1. Criar novo arquivo SQL em `sql/` com próximo número sequencial (`006_...sql`, `007_...sql`, etc.).
2. Garantir idempotência do SQL.
3. Registrar o arquivo novo em `MIGRATIONS` (ordem importa).
4. Rodar localmente:
   - `python -m app.cli migrate-workspace <workspace_uuid_lab>`
5. Validar no banco (tabela/coluna/índice/constraint criada).
6. Rodar teste de regressão relevante (`pytest ...`).
7. Aplicar em todos os workspaces ativos:
   - `python -m app.cli migrate-all`
8. Validar cobertura de schemas `ws_*` e registrar evidência no PR/commit.

## Padrões SQL recomendados

### Nova tabela

- `CREATE TABLE IF NOT EXISTS ...`
- índices com `CREATE INDEX IF NOT EXISTS ...`
- constraints explícitas e claras

### Nova coluna

- `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...`

### Ajuste de constraint/check

- `ALTER TABLE ... DROP CONSTRAINT IF EXISTS ...`
- `ALTER TABLE ... ADD CONSTRAINT ...`

## Checklist de segurança antes de aplicar em massa

- [ ] SQL é idempotente
- [ ] Não altera objetos fora de `orch_*`
- [ ] Não toca em `alembic_version`
- [ ] Migration registrada em `MIGRATIONS`
- [ ] Validada em workspace LAB
- [ ] Backup/janela operacional alinhada (produção)

## Troubleshooting

### "Rodei migrate-all e não apareceu tabela nos ws_*"

Verificar:

1. Se o processo realmente conectou no banco correto (`.env`).
2. Se o workspace está ativo/completed em `target.workspaces`.
3. Se a migration entrou em `MIGRATIONS`.
4. Se a transação foi concluída (commit) por workspace.
5. Conferir existência com `information_schema.tables` para `table_schema LIKE 'ws_%'`.

## Comandos úteis de verificação

```sql
-- Workspaces ativos/completed
SELECT workspace_uuid
FROM target.workspaces
WHERE deleted_at IS NULL
  AND COALESCE(provision_status, '') ILIKE 'completed';

-- Quantos schemas ws_* já têm orch_sessions
SELECT COUNT(*)
FROM information_schema.tables
WHERE table_schema LIKE 'ws_%'
  AND table_name = 'orch_sessions';

-- Quantos schemas ws_* já têm controle de versão do orch
SELECT COUNT(*)
FROM information_schema.tables
WHERE table_schema LIKE 'ws_%'
  AND table_name = 'orch_alembic_version';
```
