# Webhook de tabulação de voz Live → ORCH

## Estado e classificação

`ALPHA_FIX_REQUIRED`, aditivo. O endpoint recebe diretamente do Atendimento
Live a tabulação de uma chamada de voz originada pelo ORCH. A ponte existente
Runner v5 → ORCH permanece inalterada.

## Endpoint

```http
POST /v1/orch/{workspace_uuid}/{flow_uuid}/sessions/{orch_session_uuid}/live/tabulations
Content-Type: application/json
Accept: application/json
```

Por decisão operacional de 2026-10-08, a aplicação não exige credencial nesse
webhook. A restrição de tráfego entre Live e ORCH pertence à infraestrutura da
TI e precisa estar vigente no ingress antes da ativação. O endpoint não usa o
alias curto nem o workspace legado.

As três identidades da URL são obrigatórias e exatas:

- `workspace_uuid` seleciona o schema do workspace;
- `flow_uuid` precisa ser o flow da sessão;
- `orch_session_uuid` precisa ser a sessão que originou a chamada.

O envelope repete `workspace_id` e `interaction_id`. Divergência com a URL é
rejeitada com `422`; sessão inexistente no flow informado retorna `404`. Não há
correlação ou fallback por telefone.

## Envelope recebido

```json
{
  "type": "live.conversation.resolved",
  "workspace_id": "11497cd6-0332-49bb-a9f9-de0addca0114",
  "interaction_id": "3d942250-f02e-498f-a416-0958808cf930",
  "conversation_id": "01a11b37-9892-7ac1-a5e2-b785bc86b6d1",
  "reason": "agent_closed",
  "idempotency_key": "agent-close-voice:7667ad7d-9b7a-41fd-9aa6-788798522176:01a11b37-9892-7ac1-a5e2-b785bc86b6d1:state-changed",
  "tabulation_event_id": "cmuopcbf701vtqg015yal145w",
  "disposition_code": "recusa",
  "disposition_category": "negative",
  "polarity": "negative",
  "is_cpc": false,
  "value": null,
  "notes": null,
  "ends_session": true,
  "additional_data": {"isCpc": false, "valueBrl": null}
}
```

`conversation_id` é a conversa interna do Live e serve para auditoria. Campos
novos são aceitos e preservados no `data` do callback. `occurred_at` e
`call_id` são opcionais e, quando enviados, também são preservados.

`disposition_code` é opcional. Quando contém texto, o ORCH acrescenta o alias
normalizado `outcome=<disposition_code em minúsculas>` para compatibilidade com
flows existentes. Quando está ausente, o ORCH não fabrica outcome; os cards
`CONDITION` podem avaliar os demais campos e seus caminhos inesperados.

## Invariante de lifecycle

`ends_session` não possui autoridade no ORCH. O campo é aceito pelo parser para
compatibilidade e descartado antes do receipt e antes do callback. Ele:

- não encerra a sessão ORCH;
- não altera `state`, `ended_at`, cursores ou cards;
- não desbloqueia espera;
- não participa da idempotência.

O lifecycle continua pertencendo exclusivamente ao grafo ORCH.

## Normalização interna

Uma tabulação nova e válida é anexada à sessão exata como:

```json
{
  "event_name": "callback",
  "result": "tabulation",
  "data": {
    "source": "atendimento_live_direct",
    "disposition_code": "recusa",
    "outcome": "recusa"
  }
}
```

O `data` real contém também os demais campos recebidos, exceto
`ends_session`. Um `source` fornecido pelo cliente é ignorado e substituído
pelo valor canônico `atendimento_live_direct`.

A persistência reutiliza o lock e a função de callback por sessão exata já
usados pela ponte Runner. O callback é registrado mesmo quando a sessão ainda
não alcançou o `wait_for_event`; a retomada imediata só ocorre quando ela está
bloqueada em `wait_for_event(callback/tabulation)`. Sessão encerrada ou
desatribuída não é revivida.

## Receipt e idempotência

A migration `0029_create_orch_live_tabulation_events` cria o receipt
`orch_live_tabulation_events` em cada workspace. A chave única é:

```text
(orch_session_uuid, idempotency_key)
```

Comportamento:

- primeira entrega em sessão ativa: `202`, `status=applied`;
- replay com a mesma chave e conteúdo: `202`, `idempotent=true`, sem novo
  callback e sem novo enqueue;
- mesma chave com conteúdo divergente: `409`;
- sessão já encerrada/desatribuída: receipt `ignored` e `202`, sem reviver;
- sessão inexistente ou pertencente a outro flow: `404` sem receipt.

O receipt guarda o envelope normalizado sem `ends_session` e o callback
normalizado. Payload e observações não são escritos nos logs da API.

## Implantação e rollback

Como há migration por workspace e rota HTTP, a promoção deve seguir a tríade
ORCH:

1. aplicar `python -m app.cli migrate-all` uma vez no banco compartilhado;
2. confirmar a versão `0029` no workspace canário;
3. promover o contrato HTTP em rolling nos hosts `.237`, `.239` e `.249`;
4. manter o envio Live desligado até a rota responder no canário;
5. confirmar no receipt e na sessão o mesmo `idempotency_key`, resultado e
   ausência de efeito de `ends_session`;
6. habilitar primeiro somente o workspace/flow canário.

Rollback de tráfego: desligar o envio no Live. Não apagar receipts. Rollback de
aplicação pode manter a tabela `orch_live_tabulation_events`, que é aditiva e
inerte sem chamadas à nova rota.
