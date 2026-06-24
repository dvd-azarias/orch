# Integração Supplier -> ORCH (evento de `resubmit`)

## Objetivo

Padronizar a integração do Supplier com o ORCH para cenários de **resubmissão de contato**.

Quando o Supplier marca um contato como resubmetido apenas na sua lógica interna, o ORCH não cria automaticamente uma nova sessão de orquestração. Isso gera eventos posteriores (ex.: discador/hangup) sem sessão ativa correspondente.

Este documento define:

- o problema identificado;
- o endpoint de integração no ORCH;
- contrato de payload;
- comportamento de idempotência;
- orientações práticas para o time do Supplier.

---

## Problema identificado

### Cenário atual

1. Na carga inicial (`source_list`/`contact_list_members`), o ORCH cria sessão e processa fluxo normalmente.
2. Em `resubmit`, o Supplier altera estado na base compartilhada, mas **não notifica o ORCH**.
3. ORCH permanece sem nova sessão ativa para aquele ciclo.
4. Eventos de retorno chegam ao ORCH e podem ser descartados por ausência de sessão ativa.

### Impacto

- divergência de estado entre Supplier e ORCH;
- callbacks/eventos ignorados;
- execução inconsistente de componentes posteriores do fluxo.

---

## Solução implementada no ORCH

## Endpoint

`POST /v1/orch/{workspace_uuid}/{flow_uuid}/resubmit`

### Autenticação (obrigatória)

Headers:

- `x-client-id`
- `x-client-secret`

Credenciais aceitas no ORCH (ambiente):

- `SYNC_WS_CLIENT_ID` + `SYNC_WS_CLIENT_SECRET`
- ou `ARQUIVOS_CLIENT_ID` + `ARQUIVOS_CLIENT_SECRET`

Se inválidas: `401 Unauthorized`.

---

## Contrato de request

## Body JSON

Campos recomendados:

```json
{
  "event_id": "9d6f0c3a-5fd6-49b8-8a7a-7c8f4f9e6f90",
  "contact_channel_address": "5511975620806",
  "contact_identifier": "30392286855",
  "person_uuid": "f6bc7f69-44b9-4bf0-a1f5-6d7ed7d9e123",
  "external_identifier": "ext-123",
  "contact_name": "Nome Contato",
  "contact_list_member_id": "1aff1a38-d7e7-4aa9-b267-8cfc7ee9d270",
  "contact_list_id": "1aff1a38-d7e7-4aa9-b267-8cfc7ee9d270",
  "mailing_id": "9e5a4f52-3b08-4d8e-a4cf-1a4b8a1d9ef0",
  "reason": "resubmit_manual",
  "payload": {
    "supplier_context": "qualquer dado adicional"
  }
}
```

### Regras de campos

- `event_id` (**obrigatório**): chave de idempotência do evento de resubmit.
- `contact_channel_address` (**obrigatório**): telefone/endereço do contato.
- Demais campos: opcionais, mas fortemente recomendados para rastreabilidade.

---

## Comportamento do ORCH

Ao receber o `resubmit`, o ORCH:

1. valida credenciais de cliente;
2. normaliza `contact_channel_address`;
3. verifica idempotência por `event_id`:
   - se já processado, retorna sucesso sem recriar sessão;
4. desassocia sessões antigas do mesmo `entity_address` no flow (`unassign`);
5. cria nova sessão ORCH e dispara bootstrap/execução padrão do workflow.

---

## Respostas esperadas

### Sucesso (novo processamento)

`202 Accepted` com `OrchTriggerAccepted`:

- `accepted = true`
- `persistence = "saved"`
- `session_created = true|false`
- `workflow_execution.resubmit.event_id` presente

### Sucesso idempotente (replay)

`202 Accepted`:

- `accepted = true`
- `persistence = "idempotent_replay"`
- `session_created = false`
- `workflow_execution.reason = "resubmit_event_already_processed"`

### Erros principais

- `401`: credenciais de cliente inválidas
- `422`: payload inválido (`event_id`/`contact_channel_address` ausentes ou inválidos)

---

## Exemplo `curl`

```bash
curl -X POST "https://orch.otima.digital/v1/orch/<workspace_uuid>/<flow_uuid>/resubmit" \
  -H "Content-Type: application/json" \
  -H "x-client-id: <CLIENT_ID>" \
  -H "x-client-secret: <CLIENT_SECRET>" \
  -d '{
    "event_id": "9d6f0c3a-5fd6-49b8-8a7a-7c8f4f9e6f90",
    "contact_channel_address": "5511975620806",
    "contact_identifier": "30392286855",
    "contact_list_member_id": "1aff1a38-d7e7-4aa9-b267-8cfc7ee9d270",
    "reason": "resubmit_manual",
    "payload": {
      "source": "supplier"
    }
  }'
```

---

## Recomendação para o time Supplier

1. Disparar esse endpoint **sempre** que um contato for resubmetido.
2. Garantir `event_id` único por ação de resubmit.
3. Reenviar com mesmo `event_id` em caso de retry (idempotência segura).
4. Registrar `session_uuid` retornado para troubleshooting cruzado.

---

## Resumo

O `resubmit` precisa ser um evento explícito para o ORCH, não só uma mudança de estado no Supplier.  
Com esse endpoint, os dois sistemas passam a compartilhar o mesmo ciclo de sessão e evitam perda de eventos por falta de sessão ativa.

