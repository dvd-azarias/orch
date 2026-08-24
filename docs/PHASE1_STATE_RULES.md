# orch — Regras finais de `state` e `ended_at` (Fase 1)

> Documento historico da Fase 1. Retomadas de WhatsApp/Dialer, callbacks e execucao assincrona evoluiram depois e ja nao obedecem integralmente este mapeamento. Consulte `PROJECT_BRAIN.md` e `docs/project-knowledge/DATA_FLOW.md` antes de diagnosticar o runtime atual.

## Objetivo

Congelar as regras de transição de estado para manter previsibilidade operacional na fase 1.

## Mapeamento oficial de estado

- `0 = pending`
- `1 = running`
- `2 = waiting`
- `3 = finished`

## Regras por App/evento

### `DialerApp`

- Eventos de `hangup` com classificação final (`success`, `busy`, `rejected`, `invalidnumber`, `noanswer`, `failure`) fecham a sessão:
  - `state = 3`
  - `ended_at = timestamp do evento` (preferência por `EndTime`, fallback para `StartTime`, depois `now`)

### `WhatsApp`

- `sent` e `delivered`:
  - `state = 2`
  - `ended_at` permanece nulo
- `read` e `failed`:
  - `state = 3`
  - `ended_at = timestamp do status`

### `ArquivosApp` e `GenericApp`

- Eventos válidos de ingestão:
  - `state = 1`
  - `ended_at` permanece nulo

## Regras de consistência

- Não regredir estado em updates concorrentes: `state = GREATEST(state, novo_state)`.
- Sessão ativa para reuso é `state <> 3`.
- Em corrida/out-of-order, reaproveitar sessão mais recente pela chave:
  - `flow_uuid + entity + entity_type + entity_address + entity_session_id`

## Fora de escopo nesta fase

- Máquina de estados customizável por fluxo.
- Reabertura explícita de sessão `finished` com política temporal.
- SLA de expiração automática por `frozen_until`.
