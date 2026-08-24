# Divida Tecnica

## PRODUCTION_RISK

- Corrigir transacao/commit do dispatcher e cobrir com teste de integracao.
- Corrigir helper de lock do reconciliador FileApp e remover monkeypatch que mascara o caminho real.
- Alinhar unit systemd FileApp com a fila de associacao.
- Isolar schedules dos dois beats.
- Restaurar suite verde atualizando chamadas da rota legada sem alterar contrato HTTP.
- Confirmar e documentar perimetro de autenticacao dos endpoints.
- Impedir que FileApp finalize/mova arquivo sem associacao garantida ou recovery duravel.
- Reconciliar `detach_all_files` com a invariante `mailing_ids_removed=[]`.

Esses itens sao candidatos `ALPHA_FIX_REQUIRED`, mas o onboarding nao os implementa.

## FIX_SOON

- Tornar install systemd nao destrutivo.
- Adicionar pre-requisitos `rg` e Node ou validacao explicita de ausencia.
- Melhorar health por familia de worker/fila.
- Fazer smoke aguardar estado/efeito quando o objetivo for regressao.
- Adicionar log canonico `decision=fileapp_tipo1|tipo2`.
- Separar decisao FileApp da flag de transporte Celery.

## FIX_IF_TOUCHED

- Incluir workspace em advisory locks quando houver risco cross-schema.
- Reduzir I/O sincronico e transacoes longas nos componentes externos.
- Adicionar checksum/lock ao runner de migrations se uma mudanca de migration for necessaria.
- Consolidar cursor duplicado entre colunas e runtime apenas se uma manutencao exigir.

## IGNORE_UNTIL_V2

- Substituir SQL textual por ORM/repositories novos.
- Reorganizar o monolito de `workflow_m2_service.py` apenas por elegancia.
- Modernizar dependencias sem necessidade operacional.
- Redesenhar toda a arquitetura Celery/flow engine.
- Eliminar toda duplicacao historica de migrations.
