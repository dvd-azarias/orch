# Playbook de Migrations (orch)

Este documento define o **processo obrigatório** para qualquer alteração de estrutura de banco do `orch`.

## Objetivo

Garantir mudanças de schema com segurança em ambiente multi-workspace (`ws_*`), sem impacto na aplicação core e sem tocar em objetos fora do escopo `orch`.

## Regras de Ouro

- **Nunca** alterar tabelas da aplicação core.
- **Nunca** alterar enums/tabelas que pertençam a outra aplicação (mesmo dentro de `ws_*`).
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

## Nota operacional (Fase 5 / generate_file)

- A migration `0006_create_orch_generate_file_tables` cria tabelas isoladas do `orch`:
  - `orch_generate_file_job`
  - `orch_generate_file_row_buffer`
  - `orch_generate_file_dispatch_audit`
- Essas tabelas existem para evitar interferência com tabelas legadas de outras aplicações no mesmo ambiente.

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

## Handoff de ownership: `contact_list_members.linked_actuator`

Contexto:
- `contact_list_members` e o enum `linked_actuator_enum` são de ownership da API parceira (Target Core), não do ORCH.
- O ORCH **não deve mais** aplicar migrations para adicionar valores nesse enum.
- Workspaces que já receberam alterações antigas via ORCH permanecem como estão (sem rollback estrutural).

Contrato entre equipes (daqui em diante):
- ORCH apenas **consome** valores já existentes no enum.
- API parceira é responsável por criar novos valores de enum antes de publicar código que os utilize.

SQL recomendado para a API parceira (idempotente):

```sql
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_enum e
    JOIN pg_type t ON t.oid = e.enumtypid
    WHERE t.typname = 'linked_actuator_enum'
      AND e.enumlabel = 'whatsapp_without_limit'
  ) THEN
    ALTER TYPE linked_actuator_enum ADD VALUE 'whatsapp_without_limit';
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_enum e
    JOIN pg_type t ON t.oid = e.enumtypid
    WHERE t.typname = 'linked_actuator_enum'
      AND e.enumlabel = 'whatsapp_without_limit_by_rate_limit'
  ) THEN
    ALTER TYPE linked_actuator_enum ADD VALUE 'whatsapp_without_limit_by_rate_limit';
  END IF;
END
$$;
```

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

### "A migration saiu com especificação errada (Fase 6.1)"

Caso real: na Fase 6.1 a intenção era criar `assigned_at` e `unassigned_at` em `orch_sessions`, mas a primeira migration criou colunas diferentes.

Como corrigir sem retrabalho:

1. **Não editar migration já aplicada** (`0007`).
2. Criar **nova migration corretiva** (`0008`) com:
   - `ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ NULL`
   - `ADD COLUMN IF NOT EXISTS unassigned_at TIMESTAMPTZ NULL`
   - `DROP COLUMN IF EXISTS assigned`
   - `DROP COLUMN IF EXISTS unassigned_in`
3. Registrar a corretiva em `MIGRATIONS`.
4. Aplicar primeiro em 1 workspace de teste (`migrate-workspace`).
5. Validar schema final no banco.
6. Só depois aplicar em massa.

Lição prática: em ambiente com muitos workspaces, **validar cobertura pendente antes de `migrate-all`** evita execução longa e "silenciosa".

Cobertura por versão (prática recomendada):

- usar script de checagem (Python/SQLAlchemy) para listar workspaces sem a versão alvo em `ws_<uuid>.orch_alembic_version`;
- se faltarem poucos, aplicar cirurgicamente com `python -m app.cli migrate-workspace <workspace_uuid>`;
- usar `python -m app.cli migrate-all` apenas quando a fila de pendências for ampla.

> Observação: foi exatamente essa estratégia que fechou a Fase 6.1 com segurança, após detectar poucas pendências residuais da `0008`.

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
