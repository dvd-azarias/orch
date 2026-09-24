# Relatorios de Orquestracao — handoff pendente para Metrics API

## Estado

- Status: `PAUSED_AWAITING_METRICS_CONTRACT`.
- Data da decisao: 2026-09-24.
- Ownership da Metrics API: persistencia, consultas, agregacoes, API de
  relatorios e UI.
- Ownership futuro do ORCH: emitir eventos confiaveis conforme o contrato
  oficial da Metrics API.
- Nao existe migration, deploy, cutover, API ou UI desta frente em producao.
- A homologacao do flow canario pode e deve continuar independentemente deste
  handoff.

## O que deve ser preservado

O trabalho anterior confirmou invariantes que continuam uteis para qualquer
contrato externo:

```text
Sessao de orquestracao
  -> zero ou muitos acionamentos
       -> zero ou muitos eventos de provedor
```

1. Passagem por card nao equivale a acionamento externo.
2. Voz produz uma ocorrencia por tentativa real, nao por ciclo/card.
3. SMS e RCS produzem uma ocorrencia por dispatch V2.
4. WhatsApp produz uma ocorrencia por mensagem outbound real.
5. Reentrada no mesmo card cria outra tentativa com ordinal proprio.
6. Callback atualiza uma tentativa previamente emitida; nao cria tentativa
   retroativa.
7. Duplicidade e callbacks fora de ordem nao podem inflar contagens nem
   rebaixar um resultado mais recente.
8. Revisao do flow, `component_ref_id` e identidade da sessao sao dimensoes
   obrigatorias.
9. Payload de mensagem, token, credencial e dados brutos do provedor nao
   devem sair do ORCH.
10. Dado desconhecido ou nao suportado nao pode ser convertido em zero.
11. A integracao nova nao deve reconstruir ou reenviar historico anterior ao
    marco definido com a Metrics API.

## Prototipo local: classificacao

Esta branch contem um prototipo tecnico para validar as invariantes antes da
mudanca de ownership:

- migration `0023_create_orch_channel_reporting`;
- tabelas locais de estado e actions;
- repository/service de escrita fail-open;
- comando local de ativacao;
- testes de idempotencia, cutover e ordem de callbacks;
- contrato e exemplos de API/UI originalmente planejados.

Esse codigo **nao e candidato a merge** enquanto a Metrics API for a fonte de
verdade. Ele pode ser consultado para reaproveitar:

- nomenclatura e correlacao;
- mascaramento;
- transicoes de ciclo de vida;
- casos de teste;
- comportamento fail-open, idempotente e sem backfill.

Nao reaproveitar automaticamente:

- schema ou migration;
- persistencia local;
- comando de cutover;
- endpoints de relatorio;
- BFF ou UI planejados.

## Contrato necessario da Metrics API

Antes de escrever o publisher no ORCH, obter e revisar:

1. endpoint, exchange/queue ou SDK oficial;
2. autenticacao, rotacao de segredo e escopo por workspace;
3. versao do envelope e politica de compatibilidade;
4. tipos de evento aceitos;
5. campos obrigatorios e opcionais;
6. chave de idempotencia e escopo de deduplicacao;
7. ACK, timeout, retry, backoff, DLQ e replay;
8. tamanho maximo, batching e rate limits;
9. timestamps esperados: ocorrido, recebido e processado;
10. correlacao de sessao, action/tentativa e evento do provedor;
11. regras para callbacks duplicados, atrasados e fora de ordem;
12. politica de PII, mascaramento e retencao;
13. semantica de marco zero e proibicao ou nao de historico;
14. ambientes, credenciais de homologacao e observabilidade do ingest;
15. criterios de aceite na UI Metrics.

## Campos candidatos, ainda nao vinculantes

O contrato anterior sugere avaliar pelo menos:

- `event_id` e `event_type`;
- `occurred_at`;
- `workspace_uuid`;
- `flow_uuid` e `flow_revision_id`;
- `session_id` e `session_uuid`;
- `person_uuid`, quando conhecido;
- `component_ref_id` e `component_kind`;
- `channel`;
- `action_sequence`;
- `source_kind` e `source_id`;
- `lifecycle_status` e `native_outcome`;
- `provider_reference`, quando permitido;
- destino somente mascarado, se realmente necessario para a Metrics.

Esses nomes nao devem virar codigo antes de serem comparados com a
documentacao oficial.

## Estrategia de retomada

Quando a documentacao chegar:

1. criar branch nova a partir do `main` atualizado;
2. comparar o contrato oficial com este handoff e com
   `ORCHESTRATION_REPORTING_API_CONTRACT.md`;
3. produzir matriz `campo Metrics x fonte ORCH x momento de emissao`;
4. decidir transporte e garantia de entrega sem bloquear os canais;
5. instrumentar primeiro um canal em canario;
6. comprovar idempotencia, ordem, retry e visibilidade na UI Metrics;
7. expandir cirurgicamente para SMS, RCS, WhatsApp e voz;
8. documentar o rollout e somente entao encerrar esta pendencia.

## Evidencias preservadas

- Migration prototipo executada duas vezes em PostgreSQL Docker isolado:
  criacao e idempotencia confirmadas, sempre em estado `pending`, sem actions.
- Testes focados finais: `15 passed`.
- Teste PostgreSQL transacional confirmou ativacao unica, action idempotente,
  callback desconhecido sem criacao e preservacao do resultado mais recente
  em callbacks fora de ordem.
- Suite ampla do branch: `945 passed`, `26 failed`; as 26 falhas compartilham
  o contrato de teste stale `trigger_orch(flow_uuid=...)` ja divergente de
  `origin/main` e nao foram causadas pelos arquivos desta frente.

## Retorno imediato ao objetivo principal

- Workspace: `ba7eb0ec-e565-447c-8c11-8f870cf72a60`.
- Flow: `f77b70f0-849b-4d11-9ccc-449b3c4ba981`.
- Ultima evidencia: sessao `8561`, UUID
  `c9bdbd81-fe05-4d60-9938-5ba790241ae7`, WhatsApp enviado, entregue e lido,
  com finalizacao observada antes do desvinculo do mailing.
- Proximo trabalho: continuar a homologacao do fluxo a partir desse ponto,
  mantendo como objetivo final o fluxo completo.
