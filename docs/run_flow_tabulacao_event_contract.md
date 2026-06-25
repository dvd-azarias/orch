# Contrato de evento para branch `tabulacao` do `run_flow`

## Objetivo

Permitir que eventos de tabulação retomem o fluxo no card `run_flow` e sigam pelo branch `tabulacao`, mesmo com sessão encerrada, desde que dentro da janela de correlação.

## Rota

- `POST /v1/orch/{workspace_uuid}/{flow_uuid}`

## Envelope recomendado

```json
{
  "event_name": "tabulacao",
  "result": "tabulacao",
  "entity": "f73e9022-19ef-4c3c-8132-1b5c12ccc646",
  "entity_type": "person",
  "entity_address": "5511975620806",
  "entity_session_id": "GW02-1782328004.3857",
  "data": {
    "tabulation_code": "X123",
    "tabulation_label": "Sem Interesse",
    "dialer_action_id": "f73e9022-19ef-4c3c-8132-1b5c12ccc646",
    "raw": {}
  }
}
```

## Regras de correlação

1. Tenta sessão ativa por `flow_uuid + entity_address` (`unassigned_at IS NULL` e `ended_at IS NULL`).
2. Se não encontrar, tenta fallback em sessão recente por janela:
   - `flow_uuid + entity_address`
   - `unassigned_at IS NULL`
   - `created_at >= NOW() - janela`
3. Para fallback, exige card `run_flow` único no flow.
4. Quando encontra no fallback:
   - reabre sessão finalizada (`state=1`, `ended_at=NULL`);
   - posiciona cursor em `run_flow` (`last_card_uuid`/`next_card_uuid`);
   - injeta callback com `result="tabulacao"`.

## Janela de correlação

- `WORKFLOW_DIALER_EVENT_CORRELATION_WINDOW_HOURS` (default `24`).

## Resultado esperado

- O `run_flow` resolve branch por `callback.result`.
- Com `result="tabulacao"`, segue no branch `tabulacao`.
