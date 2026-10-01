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

Processos esperados: API, worker/beat workflow, worker FileApp, worker/beat
generate-file e, quando a dashboard estiver habilitada, worker exclusivo
`orch_journey_snapshot` mais um unico Beat publicando seu scanner.

O billing batch nao integra ainda a stack DEV canonica e nasce desligado. Para sua validacao/rollout, seguir exclusivamente `docs/BILLING_BATCH_RUNBOOK.md` e usar a fila local isolada do profile ativo.

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

O acesso, as credenciais, o caminho real e o inventario completo das 21 units estao na secao `ACESSO RAPIDO A PRODUCAO — HOST 10.1.20.237` de `PROJECT_STEWARD.md`.

Baseline confirmada e atualizada em 2026-09-06:

- projeto, virtualenv e `.env`: `/etc/gohp/orch`, `/etc/gohp/orch/venv` e `/etc/gohp/orch/.env`;
- cinco workers workflow consomem `orch_dispatch`, `orch_execute` e `orch_heartbeat`;
- cinco workers FileApp consomem tambem `orch_fileapp_mailing_assoc`, alem das filas ingest/process;
- cinco workers generate-file consomem run/scan;
- quatro beats separados executam workflow, FileApp rescue/higiene, generate-file scan e billing;
- um worker billing consome a fila dedicada `orch.billing.outbox`;
- API FastAPI/Uvicorn executa na porta `7777`.

Os templates em `systemctl/` ainda representam uma topologia generica antiga, com caminhos `/opt/orch`, environment file `/etc/orch/orch.env`, hostnames `136` e units sem escala horizontal. Nao instalar ou copiar esses templates diretamente sobre o `237`.

As units de billing fazem parte da baseline observada, mas continuam sendo uma familia operacional separada. Nao reinicia-las junto com workflow, FileApp ou generate-file sem que a mudanca afete explicitamente o billing.

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

## Ativação de flow/workspace no Supplier V2

Discador, materialização SMS/RCS, execução real no Target Core e callbacks no
pool HTTP possuem gates independentes. Antes de habilitar um novo flow ou
workspace, seguir integralmente
`docs/project-knowledge/SUPPLIER_V2_FLOW_ACTIVATION_RUNBOOK.md`.

Em particular, `.239/.249` hospedam duas aplicações e dois arquivos distintos:
`/etc/gohp/target-core/.env`, que governa o envio real SMS/RCS, e
`/etc/gohp/orch/.env`, que governa os callbacks ORCH distribuídos. Alterar um
não configura o outro.

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

Para billing batch, o rollback especifico e nao destrutivo: `ORCH_BILLING_ENABLED=false`, restart dos produtores/worker/Beat e preservacao integral das tres tabelas. Nao religar o legado automaticamente. Ver `docs/BILLING_BATCH_RUNBOOK.md`.

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

## Dashboard de jornadas por WebSocket

Rollout seguro:

1. aplicar as migrations `0023` e `0024` primeiro somente no workspace
   canario e conferir tabelas/constraints;
2. configurar `CELERY_JOURNEY_SNAPSHOT_WORKSPACE_UUID` para o HighComm e usar
   a fila exclusiva do profile;
3. subir o worker com hostname explicito e habilitar o scanner em apenas um
   Beat;
4. reiniciar a API para carregar ticket/gateway e confirmar que Redis
   indisponivel recusa ticket sem afetar sessoes;
5. validar snapshot no PostgreSQL, sequencia crescente, ticket de uso unico,
   Upgrade pelo BFF, reconexao e filtro pelo mesmo socket;
6. reconciliar fatos, snapshot e UI antes de remover o escopo canario.

Rollback: `ORCH_JOURNEY_DASHBOARD_ENABLED=false`, desligar o scanner e o worker
dedicado e reiniciar API/workers que escrevem telemetria. Preservar as tabelas
e o ultimo snapshot; nao executar migration reversa nem apagar fatos. PDIAL e
`dialer_metrics` ficam fora desse mecanismo.

## Eventos REST de orquestracao para a Metrics API

Rollout seguro e separado do WebSocket:

1. aplicar as migrations `0025` e `0026` primeiro somente no workspace
   HighComm e validar constraints, indices e segunda execucao idempotente;
2. implantar ORCH, Target Core Supplier e ORCHESTRATOR com o gate ainda
   desligado; reiniciar apenas as familias afetadas;
3. configurar no ORCH a base Metrics terminada em `/api`, API key fora do Git,
   allowlist contendo somente `ba7eb0ec-e565-447c-8c11-8f870cf72a60` e fila
   exclusiva `orch_metrics_events`;
4. subir um worker exclusivo da fila e habilitar
   `CELERY_BEAT_METRICS_EVENTS_ENABLED=true` em exatamente um Beat. Todos os
   outros Beats devem manter a flag `false`;
5. ativar `ORCH_METRICS_EVENTS_ENABLED=true` e reiniciar API, workers de
   workflow, worker Metrics e o Beat escolhido;
6. no canario `f77b70f0-849b-4d11-9ccc-449b3c4ba981`, comprovar outbox,
   batches HTTP, retries, eventos de sessao/no e dispatches reais de voz,
   WhatsApp, SMS e RCS;
7. comparar voz evento a evento com o PDIAL. `dialing` deve nascer apenas do
   aceite 2xx do MakeCall e o terminal deve vir do CDR/Hangup pela Supplier V2;
8. confirmar que indisponibilidade da Metrics apenas acumula outbox e nao
   interrompe sessao, Supplier, callbacks ou WebSocket.

Rollback nao destrutivo: definir `ORCH_METRICS_EVENTS_ENABLED=false`, desabilitar
o scanner no Beat e parar o worker exclusivo. Preservar outbox e snapshots para
auditoria; nao apagar eventos nem reverter as migrations.

Reparacao historica e uma operacao separada. Nao executar backfill em massa: reconstruir a relacao sessao/lista pelo payload, conferir ownership externo e aplicar updates guardados por IDs aprovados.

### Piloto de cardinalidade por pessoa

1. implantar e reiniciar primeiro o ORCH, mantendo o Target Core sem UUIDs na allowlist;
2. confirmar que payload com membro/endereço conflitante termina em `contact_member_scope_not_found` e que card de comunicação sob `person` produz `workflow_m2_person_scope_channel_component_not_supported`;
3. configurar no Target Core somente `ORCHESTRATOR_PERSON_SCOPE_FLOW_UUIDS=["e94783ce-74f0-4df6-b868-a4b17f38e1e1"]` e reiniciar API/workers que publicam a associação;
4. em nova associação do mailing, comparar `candidate_members`, `distinct_persons`, `sessions_requested` e `suppressed_by_grouping` no log Target;
5. no ORCH, confirmar uma sessão por pessoa, `input_payload.session_scope=person`, membro/endereço/tipo coerentes e ausência de alteração de `linked_actuator` por simples criação da sessão;
6. antes de ampliar a allowlist, executar um controle fora dela e comprovar que N pessoas com M canais continuam produzindo N×M sessões.

Rollback do piloto: retirar o UUID da allowlist e reiniciar o Target Core. Não é necessário desligar o roteamento contextual do ORCH; ele também protege o modo `channel`. Desassociar/reassociar ou reparar sessões existentes é operação separada e exige autorização.

## UI operacional de Gestão de Extensões no `10.1.20.239`

Esta UI é adjacente ao ORCH, mas possui runtime e deploy próprios. Não reiniciar
serviços ORCH para uma alteração exclusiva da interface.

- units: `dialing-management-demo-ui.service` e
  `dialing-management-demo-bff.service`;
- entrada autenticada: `10.1.20.239:8300`;
- runtime obrigatório: Node dedicado `22.17.0`;
- Node global `18.19.1`: proibido para build;
- arquitetura: navegador -> BFF restrito -> APIs v2 Target Core, sem acesso
  direto ao banco;
- inspeção visual canônica: instalar `@playwright/cli` e abrir com
  `playwright-cli open http://10.1.20.239:8300/`; autenticação automatizada usa
  configuração temporária fora do Git, nunca credenciais na URL;
- release ativa confirmada em 2026-09-13:
  `20260913T125615-telecom-response-ui`.

Antes de investigar, alterar, implantar ou reverter essa UI, ler integralmente
`docs/project-knowledge/DIALING_MANAGEMENT_UI_RUNBOOK.md`. A fonte oficial é o
repositório privado `GOHP-LAB/target-extensions-ui`; nenhuma nova funcionalidade
deve partir de `/private/tmp`, do servidor ou da working copy histórica.

# Ponte Runner v5 → ORCH

- Confirmar migration `0027` em todos os workspaces antes de permitir tráfego.
- Correlacionar pelos campos `runner_session_id`, `event_key`,
  `provider_context_message_id` e `orch_session_id`; nunca forçar vínculo por
  telefone.
- `pending_link` pode ser consistência eventual; `conflict` é fail-closed e
  exige investigação. `ignored/orch_session_inactive` prova que a sessão não
  foi revivida.
- Em incidente, desligar o rollout no Target Core. Não truncar as tabelas de
  receipts/links e não alterar o endpoint genérico de callback.
