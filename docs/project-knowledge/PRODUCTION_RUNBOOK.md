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

## Linux/systemd — producao `10.1.20.237`

O acesso, as credenciais, o caminho real e o inventario completo das 19 units estao na secao `ACESSO RAPIDO A PRODUCAO — HOST 10.1.20.237` de `PROJECT_STEWARD.md`.

Baseline confirmada:

- projeto, virtualenv e `.env`: `/etc/gohp/orch`, `/etc/gohp/orch/venv` e `/etc/gohp/orch/.env`;
- cinco workers workflow consomem `orch_dispatch`, `orch_execute` e `orch_heartbeat`;
- cinco workers FileApp consomem tambem `orch_fileapp_mailing_assoc`, alem das filas ingest/process;
- cinco workers generate-file consomem run/scan;
- tres beats separados executam workflow, FileApp rescue/higiene e generate-file scan;
- API FastAPI/Uvicorn executa na porta `7777`.

Os templates em `systemctl/` ainda representam uma topologia generica antiga, com caminhos `/opt/orch`, environment file `/etc/orch/orch.env`, hostnames `136` e units sem escala horizontal. Nao instalar ou copiar esses templates diretamente sobre o `237`.

Diagnosticar pela configuracao efetiva:

```bash
systemctl list-unit-files 'orch-*.service' --no-pager
systemctl list-units --type=service --state=running 'orch-*.service' --no-pager
systemctl show <unit>.service -p ActiveState -p UnitFileState -p MainPID -p EnvironmentFiles -p ExecStart --no-pager
systemctl cat <unit>.service --no-pager
journalctl -u <unit>.service --since '15 minutes ago' --no-pager
```

Advertencias:

- `orch-celery-fileapp-worker.service` sem sufixo existe, mas esta desabilitada; a stack usa `_01..05`;
- os beats compartilham o mesmo `celery_app`; confirmar flags efetivas antes de atribuir um schedule exclusivamente pelo nome da unit;
- `scripts/systemd_orch.sh install` e os templates versionados nao representam a instalacao escalada atual;
- a API do `10.1.20.136` permanece ativa temporariamente por dependencia do proxy externo; nao desabilita-la sem confirmacao;
- preservar `.env`, `venv/` e alteracoes operacionais locais durante pull/troubleshooting.

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

## Rollout do roteamento contextual de membros

A correcao de selecao de `contact_list_members` nao exige migration e fica protegida por `WORKFLOW_CONTEXTUAL_MEMBER_ROUTING_ENABLED`.

Sequencia segura:

1. implantar o codigo com a flag `false` e reiniciar API/workers;
2. em filas dedicadas e com dispatch/reconcile escopados ao workspace de teste, iniciar com a flag `true`;
3. antes de habilitar em outro workspace, confirmar que `contact_list_member_id` e `mailing_id` recebidos sao numericos e que `contact_list_id` e UUID;
4. validar um caso Dialer e um WhatsApp com identificador duplicado, comparando payload, `variables.contact.contact_list_member_id`, metadata `*_routing.assignment` e a linha atualizada;
5. validar conflito deliberado: sessao deve terminar em `state=3`, `ended_at` preenchido, cursor nulo e um alarme `workflow_m2_contact_member_scope_not_found`;
6. habilitar a flag no ambiente alvo, reiniciar os processos e monitorar os dois alarmes `workflow_m2_contact_member_scope_not_found` e `workflow_m2_contact_member_routing_update_failed`.

Rollback: definir a flag como `false` e reiniciar API/workers. Isso restaura o seletor legado; nao desfaz `linked_actuator`, `ani`, consumo ou sessoes ja terminalizadas.

Reparacao historica e uma operacao separada. Nao executar backfill em massa: reconstruir a relacao sessao/lista pelo payload, conferir ownership externo e aplicar updates guardados por IDs aprovados.
