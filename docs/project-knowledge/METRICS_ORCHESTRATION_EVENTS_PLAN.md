# Integracao ORCH -> Metrics API — eventos de orquestracao

## Objetivo e fronteira

Integrar as jornadas do ORCH com o contrato da Metrics API sem substituir,
alterar ou condicionar os mecanismos WebSocket ja existentes.

Esta frente e aditiva:

- o WebSocket proprio de jornada, PDIAL e CTI Server permanecem inalterados;
- a Metrics API recebe eventos REST duraveis por uma outbox exclusiva;
- falha, lentidao ou indisponibilidade da Metrics nao bloqueia a sessao, o
  Supplier, callbacks funcionais nem o WebSocket;
- nao existe retroativo. O marco zero e a ativacao explicita por workspace;
- o ORCH emite apenas `flow_type=orchestration`;
- o sistema que recebe um handoff abre outra interacao, com outro ID e
  `flow_type=attendance`.

Classificacao: `ALPHA_FIX_OPTIONAL`, com rollout fail-closed e mudanca minima.

## Decisoes fechadas

1. `context.interaction_id` e o UUID da sessao de orquestracao.
2. `context.contact_id` usa o primeiro identificador disponivel nesta ordem:
   `person_uuid`, `contact_list_member_id`, `contact_draft_id`, identificador
   externo.
3. Voz usa exatamente a taxonomia publicada no PDIAL em todos os pontos:
   `machine`, `no_answer`, `rejected`, `answered`, `busy`, `invalid_number` e
   `failed`. Uma chamada iniciada usa ainda `dialing`.
4. Timeout interno de card nao produz `failed` de provedor.
5. Nao ha backfill.
6. Handoff encerra a participacao da interacao ORCH.
7. Cada tentativa ou fallback recebe um `dispatch_id` diferente.
8. Todos os updates de um dispatch repetem seu snapshot completo e conservam
   o mesmo `dispatched_at`.

## Arquitetura segura

```text
fato real do runtime/provedor
          |
          +--> persistencia funcional/jornada existente --> WebSocket atual
          |
          +--> outbox Metrics (mesma transacao, savepoint isolado)
                         |
                    worker dedicado
                         |
                POST /api/v1/events/ingest
```

O fato funcional e a fonte. O entregador nao infere um envio apenas porque o
cursor chegou ao card. Para canais digitais, o primeiro `sent` nasce da
confirmacao real do provedor; em voz, `dialing` nasce do MakeCall efetivo.

## Contratos de identidade e idempotencia

- `event_id`: UUID persistido na outbox.
- `idempotency_key`: chave deterministica unica por fato.
- sessao iniciada: `session:{session_uuid}:started`;
- execucao iniciada: `execution:{session_uuid}:started`;
- entrada/saida de no: inclui ID persistido da visita e transicao;
- encerramento: inclui o estado terminal da sessao;
- dispatch: inclui `dispatch_id`, status canonico e identidade do evento do
  provedor.

Retries reenviam o mesmo envelope e o mesmo `event_id`.

## Gates

### Gate M0 — contrato e ponto de retorno

- [x] preservar integralmente os WebSockets atuais;
- [x] fixar IDs, precedencia de contato e fronteira do handoff;
- [x] fixar taxonomia de voz no PDIAL;
- [x] registrar que nao existe retroativo;
- [x] registrar retorno ao canario e ao fluxo completo.

### Gate M1 — outbox duravel, ainda sem emissao funcional

- [x] migration aditiva por workspace;
- [x] configuracao desligada por padrao e allowlist explicita;
- [x] fila/worker dedicados por perfil de ambiente;
- [x] claim com lease, batch maximo 100 e `SKIP LOCKED`;
- [x] retry exponencial para rede, `429` e `5xx`;
- [x] erro permanente auditavel sem hot loop;
- [x] segredo somente em ambiente;
- [x] testes de migration, idempotencia, claim e falha independente.

Gate de saida: indisponibilidade da Metrics acumula outbox sem alterar runtime
ou WebSocket; retorno restabelecido drena sem duplicar o fato logico.

### Gate M2 — sessao, execucao e nos

- [x] `interaction.session.started.v1`;
- [x] `flow.execution.started.v1`;
- [x] `flow.node.entered.v1`;
- [x] `flow.node.exited.v1`;
- [ ] `flow.node.error.v1`: aguarda um fato persistido inequívoco no runtime;
- [x] `flow.execution.completed.v1`/`failed.v1`;
- [x] `interaction.session.ended.v1` ou evento terminal equivalente;
- [x] `flow_type=orchestration` em todos os envelopes;
- [x] validar exatamente um start e um terminal por sessao.

Gate de saida: uma sessao canaria reproduz sua jornada na Metrics sem alterar
o snapshot/WebSocket proprio.

### Gate M3 — dispatches digitais

- [x] WhatsApp: `sent`, `delivered`, `read`, `failed` e primeira resposta
  correlacionada como `replied`;
- [x] SMS: aceite real como `sent`, DLR, falha e MO como `replied`;
- [x] RCS: aceite real, entrega, leitura, falha e resposta;
- [ ] e-mail somente quando existir envio e callback reais;
- [x] snapshot completo em todo update;
- [x] destino, template e dados do contato sem expor segredos.

Gate de saida: cada tentativa gera um dispatch unico e callbacks fora de ordem
nao rebaixam o status publicado.

### Gate M4 — voz

- [x] criar/correlacionar `dispatch_id` no MakeCall real;
- [x] publicar `dialing` apenas depois do MakeCall;
- [x] publicar o terminal usando a taxonomia integral do PDIAL;
- [x] repetir snapshot e incluir duracao quando aplicavel;
- [x] preservar Supplier V1 e emissores legados;
- [x] garantir que timeout interno nao simula falha telefonica.

Gate de saida: chamada canaria possui um `dialing` e um terminal correlacionado,
sem qualquer mudanca na classificacao PDIAL.

### Gate M5 — rollout e evidencia

- [ ] habilitar somente no workspace HighComm
  `ba7eb0ec-e565-447c-8c11-8f870cf72a60`;
- [ ] usar o fluxo canario
  `f77b70f0-849b-4d11-9ccc-449b3c4ba981`;
- [ ] observar backlog, latencia, retries, HTTP e ausencia de bloqueio;
- [ ] comparar os status de voz com o PDIAL;
- [ ] validar WhatsApp, SMS e RCS com callbacks reais;
- [ ] ampliar allowlist somente depois da evidencia canaria.

### Gate M6 — retorno obrigatorio

- [ ] retomar a homologacao funcional interrompida do canario
  `f77b70f0-849b-4d11-9ccc-449b3c4ba981`;
- [ ] depois retomar e revisar o fluxo completo
  `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`;
- [ ] nao reabrir gates ja homologados sem evidencia de regressao.

## Rollback

1. desligar `ORCH_METRICS_EVENTS_ENABLED`;
2. parar beat/worker exclusivo da Metrics;
3. manter tabelas e backlog para diagnostico;
4. nao reverter sessoes, jornada, Supplier ou WebSocket;
5. corrigir e rearmar somente itens da outbox explicitamente auditados.

## Proibicoes desta frente

- nao alterar o contrato WebSocket existente;
- nao enviar `attendance` pelo ORCH;
- nao inventar envio/retorno de provedor;
- nao usar card timeout como status de canal;
- nao fazer backfill;
- nao registrar tokens, bodies com segredo ou cabecalhos de autenticacao;
- nao ativar globalmente antes do canario.
