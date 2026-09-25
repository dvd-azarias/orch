# Telemetria de Jornadas ORCH -> Metrics — contrato WebSocket

## Estado

- Versao proposta: `0.1-draft`.
- Data: 2026-09-25.
- Produtor: ORCH.
- Consumidor: Metrics API/UI.
- Transporte: WebSocket SYNC ja usado por `dialer_metrics`.
- Escopo: somente fatos posteriores ao `coverage_started_at` do workspace.
- Historico/backfill: inexistente.
- Gate: proposta do Gate 1; nao implementar runtime antes da confirmacao dos
  itens em **Decisoes pendentes da Metrics/SYNC**.

Este documento complementa `ORCHESTRATION_REPORTING_PLAN.md`. O contrato REST
anterior permanece referencia de graos e canais, mas nao e uma entrega ativa.

## Fronteira de responsabilidade

- O ORCH registra fatos duraveis, gera snapshots e publica mensagens.
- O Beat somente agenda flows com dados novos.
- Um worker e uma fila exclusivos drenam a outbox.
- A Metrics deduplica, persiste, agrega periodos arbitrarios e serve sua UI.
- O PDIAL nao calcula, agenda ou publica telemetria de jornada.
- `dialer_metrics` permanece independente e inalterado.

## Transporte comprovado

Autenticacao atual:

```json
{
  "action": "auth",
  "payload": {
    "client_id": "<fora-do-git>",
    "client_secret": "<fora-do-git>",
    "browser_info": {
      "agent": "orch",
      "version": "1.0",
      "os": "linux"
    }
  }
}
```

Resposta minima esperada da autenticacao:

```json
{
  "payload": {
    "authenticated": true
  }
}
```

Envelope de publicacao:

```json
{
  "action": "broadcast:dashboard",
  "payload": {
    "target_application": "metrics",
    "target_workspace_uuid": "<workspace_uuid>",
    "message": {}
  }
}
```

O roteamento fisico comprovado termina no workspace. A separacao por jornada
e logica:

```text
topic = orchestration.journey.{workspace_uuid}.{flow_uuid}
```

Uma sala fisica por flow somente sera adotada se a Metrics/SYNC confirmar esse
recurso e seu contrato.

## Contratos complementares

### `orchestration_journey_event`

Fato incremental e imutavel. Permite historico, filtros arbitrarios e
reprocessamento idempotente.

```json
{
  "type": "orchestration_journey_event",
  "topic": "orchestration.journey.<workspace_uuid>.<flow_uuid>",
  "meta": {
    "version": "1.0.0",
    "message_id": "<uuid>",
    "event_id": "<uuid-deterministico>",
    "stream_position": 123,
    "occurred_at": "2026-09-25T12:00:00.000Z",
    "recorded_at": "2026-09-25T12:00:00.100Z",
    "sent_at": "2026-09-25T12:00:00.500Z",
    "source_application": "orch"
  },
  "payload": {
    "event_type": "stage_entered",
    "workspace": {"uuid": "<workspace_uuid>"},
    "flow": {
      "uuid": "<flow_uuid>",
      "revision_id": "<revision_uuid>",
      "revision_version": 9
    },
    "session": {
      "uuid": "<session_uuid>",
      "scope": "person"
    },
    "component": {
      "ref_id": "<component_ref_id>",
      "kind": "identidade"
    },
    "stage": {
      "id": "identificacao",
      "ordinal": 2,
      "visit_number": 1,
      "previous_id": "entrada"
    }
  }
}
```

Tipos iniciais:

- `session_started`;
- `stage_entered`;
- `session_waiting`;
- `session_resumed`;
- `session_completed`;
- `session_abandoned`;
- `session_failed`;
- `channel_action_created`;
- `channel_action_status_changed`.

`stage_entered` carrega a etapa anterior e torna desnecessario emitir um
segundo fato `stage_transitioned`. Uma nova visita ao mesmo card/etapa recebe
novo `visit_number`, mas a Metrics conta a sessao apenas uma vez em
`reached_sessions`.

Identidade proposta do evento:

```text
UUIDv5(
  workspace_uuid,
  source_kind + source_id + event_type + native_status + visit_or_sequence
)
```

O `stream_position` e crescente no workspace e pode conter lacunas. Ordem de
negocio usa `occurred_at`; diagnostico preserva `recorded_at` e `sent_at`.

### `orchestration_journey_snapshot`

Estado agregado para carga inicial, reconciliacao e heartbeat. Nao substitui
os fatos incrementais.

```json
{
  "type": "orchestration_journey_snapshot",
  "topic": "orchestration.journey.<workspace_uuid>.<flow_uuid>",
  "meta": {
    "version": "1.0.0",
    "message_id": "<uuid>",
    "snapshot_id": "<uuid>",
    "snapshot_sequence": 42,
    "sent_at": "2026-09-25T12:00:05.000Z",
    "source_application": "orch"
  },
  "payload": {
    "as_of": "2026-09-25T12:00:05.000Z",
    "workspace": {"uuid": "<workspace_uuid>"},
    "flow": {"uuid": "<flow_uuid>", "name": "<flow_name>"},
    "window": {
      "kind": "today",
      "from": "2026-09-25T03:00:00.000Z",
      "to": "2026-09-25T12:00:05.000Z",
      "timezone": "America/Sao_Paulo"
    },
    "summary": {},
    "active_progress": {},
    "health": {},
    "funnel": [],
    "stage_dropoffs": [],
    "channels": [],
    "outcomes": [],
    "duration": {},
    "alerts": [],
    "data_quality": {}
  }
}
```

O snapshot inicial usa a janela `today`. Periodos customizados, ontem, 7 e 30
dias sao calculados pela Metrics sobre os fatos persistidos, e nao geram uma
combinacao infinita de snapshots no ORCH.

## Sete etapas canonicas

| Ordinal | ID | Rotulo |
| ---: | --- | --- |
| 1 | `entrada` | Entrada |
| 2 | `identificacao` | Identificacao |
| 3 | `qualificacao` | Qualificacao |
| 4 | `abordagem` | Abordagem |
| 5 | `proposta` | Proposta |
| 6 | `decisao` | Decisao |
| 7 | `desfecho` | Desfecho |

Cada item de `funnel` inclui:

- `stage_id`, `ordinal` e `configured`;
- `configured_cards`;
- `reached_sessions` distintas;
- `visit_count` incluindo loops;
- `transitioned_out_sessions` distintas;
- `completed_here`;
- `abandoned_here`;
- `abandonment_rate` e seu denominador;
- duracao mediana/P90 quando houver cobertura.

Etapa nao usada na revisao aparece com `configured=false` e zero. Etapa usada,
mas sem sessao no periodo, aparece com `configured=true` e zero.

## Semantica dos blocos do snapshot

### `summary`

- `sessions_started`: sessoes iniciadas na janela;
- `sessions_in_progress`: sessoes dessa coorte ainda nao terminais em `as_of`;
- `sessions_completed`: sessoes concluidas por `finish_flow`;
- `sessions_abandoned`: termino sem conclusao normal;
- `sessions_stalled`: subconjunto ativo sem progresso alem do limite;
- `conversion`: `value`, `rate` e `denominator`; sucesso depende de resultado
  terminal explicito;
- `average_duration_seconds`: somente sessoes concluidas com relogios validos.

Conclusao, conversao e travamento nao sao somados como estados mutuamente
exclusivos. Todo indicador declara denominador.

### `active_progress`

Para grafos com branches e loops, o ORCH nao inventa percentual por numero de
cards. Cada sessao publica `current_stage`, `highest_stage` e
`basis=stage_rank`. Se a UI representar percentual, deve declarar essa base.

### `health`

Categorias mutuamente exclusivas, nesta precedencia:

1. `error`;
2. `awaiting_intervention`;
3. `stalled`;
4. `delayed`;
5. `normal`.

Esperar callback automatico nao significa intervencao humana. Os thresholds
`delayed_after_seconds` e `stalled_after_seconds` acompanham o snapshot.

### `channels`

O grao e tentativa externa real:

- WhatsApp: uma mensagem outbound;
- SMS/RCS: um dispatch V2;
- Voz: uma tentativa de discagem;
- E-mail: `available=false` ate existir emissor homologado.

Cada canal informa `actions`, `sessions`, `unique_people`, estados nativos,
marcos cumulativos, falhas e desconhecidos. Card visitado nao cria action.

### `outcomes`

Colecao dinamica; nao codificar `acordo`, `recusa` ou `recado` globalmente.
Prioridade proposta:

1. resultado explicito de `finish_flow`;
2. tabulacao/disposicao normalizada;
3. falha ou abandono;
4. `unknown`.

O status nativo e preservado ao lado da categoria normalizada.

### `duration`

- media, mediana, P90 e P95 da sessao;
- faixas de duracao;
- opcionalmente permanencia por etapa;
- amostra e denominador obrigatorios.

### `alerts`

- sem progresso alem do threshold;
- espera humana acima do threshold;
- erro tecnico terminal ou recorrente;
- repeticao anormal de identificacao/etapa;
- backlog ou atraso de publicacao da propria telemetria.

## Privacidade e isolamento

- Nenhum texto de mensagem, prompt, corpo, token, segredo ou payload bruto.
- Nenhum address em claro.
- `person_uuid` somente se o contrato Metrics exigir; para distinct count,
  preferir identificador pseudonimizado com sal por workspace.
- Toda mensagem declara workspace, flow e revisao.
- Consumidor deve rejeitar divergencia entre `target_workspace_uuid` externo e
  `message.payload.workspace.uuid`.
- Dado de outro workspace nunca e aceito por fallback.

## Entrega, ACK e idempotencia

- Outbox preserva payload imutavel e publica `at-least-once`.
- Metrics deduplica evento por `event_id`.
- Snapshot mais novo substitui o anterior pela maior `snapshot_sequence` do
  mesmo workspace/flow/janela.
- Socket `send()` bem-sucedido nao comprova consumo. A linha so pode ser
  confirmada definitivamente com ACK correlacionado a `message_id`; sem ACK,
  o contrato deve definir retencao/reconciliacao explicita.
- Evento atrasado atualiza o periodo de `occurred_at`, preservando os demais
  relogios; nunca e silenciosamente movido para `received_at`.

## Decisoes pendentes da Metrics/SYNC

1. Existe ACK de `broadcast:dashboard` por `message_id`? Qual o envelope?
2. Existe sala fisica ou assinatura por flow, ou somente por workspace?
3. Qual o tamanho maximo de mensagem e compressao suportada?
4. Qual a politica do servidor para mensagem duplicada?
5. A Metrics aceita fatos individuais e snapshots, ou exige batch de fatos?
6. Qual o limite de mensagens/segundo por workspace e por conexao?
7. A Metrics armazenara os fatos para periodos customizados e callbacks
   atrasados?
8. Quais versoes de contrato podem coexistir e como sinalizar deprecacao?

O Gate 1 somente termina depois de essas respostas serem incorporadas e os
fixtures serem aceitos pela equipe consumidora.
