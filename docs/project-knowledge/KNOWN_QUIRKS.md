# Peculiaridades Conhecidas

## Documentacao por fases e historica

`README.md`, `PHASE1_STATE_RULES.md` e documentos de fase misturam estado inicial, planejamento e comportamento atual. Exemplos:

- README diz no inicio que API/Rabbit/Redis nao foram implementados, embora o repositorio os contenha;
- regras de estado da fase 1 nao refletem retomadas atuais de WhatsApp/Dialer;
- FileApp e descrito tanto como escrita local quanto como delegacao Target Core.

Use codigo executado e testes atuais antes de tratar texto antigo como contrato.

## Rota legada aceita alias

A funcao `trigger_orch` recebe `alias_or_flow_uuid`, nao `flow_uuid`. Vinte e cinco testes DB antigos ainda chamam o nome anterior e falham antes de testar comportamento.

## Deteccao ArquivosApp

O detector exige `payload.file` como dicionario. Sinais S3/MinIO isolados documentados nao classificam o payload como ArquivosApp.

## Estado de canal nao segue fase 1

O codigo atual pode reabrir sessoes finalizadas de WhatsApp e manter status em waiting para permitir retomada do workflow. Dialer tambem tem correlacao por janela. Isso e evolucao funcional, nao simples CRUD de estado.

## entity_origin_app

E historico e nao muda a cada evento. Para origem corrente, usar runtime.

## Dois formatos de cursor

Cursores aparecem em colunas UUID e em metadados string dentro de `runtime_variables.workflow_v2`. Investigar ambos ao diagnosticar.

## Migrations ausentes na sequencia

Arquivos `016/017` existem, mas foram intencionalmente retirados da lista executavel por ownership do enum no Target Core.

## Migrations retroativamente redundantes

SQLs iniciais foram atualizados depois, tornando migrations corretivas redundantes em instalacao nova. O runner nao usa checksum.

## Um app Celery, varios beats

Separar processos nao separa automaticamente schedules. Cada beat importa a mesma app e precisa de flags corretas para excluir rotinas que nao lhe pertencem.

## Hostname Celery duplicado

Scripts locais usam `--hostname` e `-n` no mesmo comando; ambos representam a mesma opcao e o ultimo pode prevalecer, divergindo do nome documentado no Flower.

## Smoke e health sao parciais

Smoke confirma aceite HTTP; health Celery aceita qualquer worker. Nenhum deles, isoladamente, prova a topologia ou o E2E.
