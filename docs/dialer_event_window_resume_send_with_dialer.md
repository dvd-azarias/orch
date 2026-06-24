# DialerApp: retomada por janela para eventos com sessão encerrada

## Contexto

Quando um evento de telefonia (`Hangup`) chegava na rota `/v1/orch/{flow_uuid}` e a sessão já estava encerrada, o ORCH descartava com:

- `run_flow_hangup_session_not_found_by_address`

Isso é correto para vários cenários, mas quebra o fluxo quando há novas tentativas do mesmo contato (ex.: políticas de `dialrules` com múltiplas tentativas).

## Regra implementada

Escopo **somente** para `DialerApp` em eventos de `hangup`.

Ordem de correlação:

1. tenta o comportamento atual (sessão ativa por `entity_address`);
2. se não encontrar, tenta fallback por **janela temporal** em sessão recente:
   - mesmo `flow_uuid`;
   - `entity_type = person`;
   - mesmo `entity_address`;
   - `unassigned_at IS NULL`;
   - `created_at >= NOW() - janela`;
   - exige existir **exatamente 1** card `send_with_dialer` no flow.

Se fallback encontrar sessão:

- injeta o callback `run_flow.hangup` nas variáveis de runtime;
- reabre sessão se estava finalizada (`state=1`, `ended_at=NULL`);
- força cursor para `send_with_dialer`:
  - `last_card_uuid = <send_with_dialer_ref_id>`
  - `next_card_uuid = <send_with_dialer_ref_id>`

## Janela de correlação

Nova configuração:

- `WORKFLOW_DIALER_EVENT_CORRELATION_WINDOW_HOURS`
- padrão: `24`

## Segurança funcional

- Não altera comportamento de canais não telefônicos (WhatsApp/Telegram/etc.).
- Não ativa fallback quando houver 0 ou mais de 1 `send_with_dialer` no flow.
- Mantém descarte quando não houver sessão correlacionável na janela.

## Impacto esperado

- Eventos de nova tentativa de discagem deixam de ser descartados apenas por sessão encerrada.
- Retomada segue dos branches do `send_with_dialer`, mantendo semântica de resposta de telefonia no próprio card.
