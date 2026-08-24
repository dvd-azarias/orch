# Runbook de Producao e Diagnostico

Este runbook consolida somente procedimentos versionados. O estado real de producao deve ser observado antes de qualquer acao.

## Antes de intervir

1. Ler `AGENTS.md`, `PROJECT_STEWARD.md`, `PROJECT_BRAIN.md` e a documentacao da area.
2. Capturar versao/commit implantado, processo que atende a porta, workers, beats, filas e workspace alvo.
3. Nao misturar `launchd` com stack manual.
4. Nao reutilizar filas compartilhadas.
5. Preservar logs, task IDs, request IDs e timestamps antes de reiniciar.

## DEV local canonico

```bash
scripts/dev_phase_stack.sh restart
scripts/dev_phase_stack.sh status
scripts/dev_phase_stack.sh smoke 5
```

Processos esperados: API, worker/beat workflow, worker FileApp e worker/beat generate-file.

Bloqueio atual conhecido: o script usa `rg` para detectar prontidao. Se `rg` nao existir, a validacao falha mesmo com worker pronto. Nao interprete esse sintoma como falha Celery sem olhar os logs.

Logs: `.runlogs/phase_stack/`.

## macOS persistente

Somente quando solicitado:

```bash
scripts/launchd_orch.sh restart
scripts/launchd_orch.sh status
```

Logs: `.runlogs/launchd/`.

## Linux/systemd

Templates versionados preveem seis units:

- `orch-api`;
- `orch-celery-worker`;
- `orch-celery-fileapp-worker`;
- `orch-celery-beat`;
- `orch-celery-generate-file-worker`;
- `orch-celery-generate-file-beat`.

Diagnostico versionado:

```bash
sudo systemctl status orch-api orch-celery-worker orch-celery-fileapp-worker orch-celery-beat orch-celery-generate-file-worker orch-celery-generate-file-beat
sudo journalctl -u orch-api -f
sudo journalctl -u orch-celery-worker -f
sudo journalctl -u orch-celery-fileapp-worker -f
```

Advertencias antes de usar templates:

- a unit FileApp nao consome `orch_fileapp_mailing_assoc`;
- o beat generate-file nao desabilita todos os reconciliadores comuns;
- `scripts/systemd_orch.sh install` copia o exemplo de env sobre o destino; revisar antes de executar;
- confirmar se as units instaladas foram alteradas manualmente.

## Health

1. `/health/live`: API viva.
2. `/health/db`: conectividade basica.
3. `/health/ready`: schema default e `orch_sessions`.
4. `/health/celery`: broker, algum worker e heartbeat Redis.

Limitacao: `/health/celery` nao comprova que todos os workers, beats ou consumidores obrigatorios estejam presentes. Validar filas/consumers separadamente no RabbitMQ/Flower.

## Smoke

O smoke do script envia GenericApp para dois flows e comprova apenas resposta HTTP nao vazia/aceite. Para regressao real, acrescentar:

- estado final ou parada esperada em `orch_sessions`;
- logs do worker com `session_id/task_id`;
- metricas/alarmes;
- efeito externo observado para `api_call`, LLM ou SFTP.

## Diagnostico de workflow parado

1. Correlacionar `request_id`, workspace, flow, session e task.
2. Consultar estado, `last_card_uuid`, `next_card_uuid`, `frozen_until`, `ended_at` e runtime.
3. Verificar ledger `orch_channel_events` e alarmes.
4. Confirmar consumidor da fila `execute` e logs do worker workflow.
5. Verificar se a revisao do flow mudou desde o bootstrap.
6. Considerar claim do dispatcher revertido e enqueues repetidos; nao assumir que `state=0` significa ausencia de dispatch.

## Diagnostico FileApp

Evidencia minima:

1. `202` com pipeline `fileapp_tipo1_ingest` ou `fileapp_tipo2_ingest`;
2. ingest recebida;
3. process concluido;
4. para tipo 1, associacao consumida e POST observado;
5. SQL final de `persons` e `orch_sessions` conforme contrato.

Checar adicionalmente:

- pasta monitorada e `processados`;
- template realmente resolvido como UUID;
- filas ingest/process/association e consumidores;
- estado do mailing/source list;
- arquivos em `falha` e possivel reingestao;
- alarmes de enqueue, import, associacao e pos-processamento.

## Migrations

Comandos oficiais:

```bash
python -m app.cli migrate-workspace <workspace_uuid>
python -m app.cli migrate-all
```

Seguir `docs/MIGRATIONS_PLAYBOOK.md`. Aplicar primeiro em workspace LAB, validar DDL/dados, depois massa. Nao tocar em `alembic_version` nem em enum de ownership externo.

## Rollback operacional

Nao ha procedimento universal versionado. Antes de qualquer mudanca, definir rollback por area. Para documentacao apenas, rollback e reverter os arquivos de conhecimento. Para runtime, nao improvisar reset, migration reversa ou purge de fila.

