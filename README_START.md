# Prompt inicial para o Codex — Projeto `orch`

---

# Pedido inicial do projeto `orch`

Você atuará como engenheiro Python sênior, especialista em FastAPI, PostgreSQL, arquitetura limpa, webhooks, processamento de payloads de terceiros e desenho evolutivo de aplicações de workflow.

Quero começar uma nova aplicação chamada **orch**.

O `orch` será uma aplicação de **workflow/orquestração**, acionada inicialmente por eventos externos recebidos via API. Nesta fase 1, o objetivo principal é receber payloads de diferentes aplicações, identificar de qual App/evento eles vieram, normalizar os dados mínimos de sessão e registrar/atualizar uma tabela PostgreSQL chamada `orch_sessions`.

## Regras essenciais de trabalho

- Fale comigo sempre em **pt-BR**.
- Não omita, não simplifique e não altere payloads de terceiros.
- Os payloads recebidos são de sistemas externos, portanto **não tenho autonomia para modificá-los**.
- Toda lógica deve se adaptar aos payloads como eles chegam.
- Sempre que houver dúvida antes de alterar arquitetura, modelos ou comportamento de negócio, pergunte.
- Existe uma pasta `.venv` dentro do projeto. Todo teste, execução, instalação de dependência e desenvolvimento deve considerar esse ambiente virtual.
- Quando precisar de elevação de permissão, apenas me peça.
- Há um arquivo `.env` com as credenciais do PostgreSQL que usaremos nesta aplicação.
- Não invente integrações da fase 2 agora.
- RabbitMQ será usado na fase 2.
- Redis poderá ser usado da fase 2 em diante.
- Nesta fase 1, SMS e RCS **não serão implementados**, mas os campos correspondentes já devem existir na tabela.
- Organize o projeto para crescer bem, mas implemente apenas o necessário da fase 1.
- Alimente/atualize o `README.md` com todas as informações importantes sobre o projeto, ambiente, forma de execução, dependências, escopo da fase 1, endpoints, regras de sessão e próximos passos.

## Stack prevista

- Python 3.12+
- FastAPI
- PostgreSQL
- RabbitMQ na fase 2
- Redis eventualmente da fase 2 em diante

## Endpoint principal de engatilhamento

A aplicação será acionada por API:

```http
POST http://ip_srv:7777/v1/orch/{flow_uuid}
```

O parâmetro `flow_uuid` virá na URL e deverá ser persistido na sessão.

A API deve aceitar payloads diversos, no estilo webhook, e tratar caso a caso os eventos possíveis.

Nesta fase 1, os Apps/eventos principais são:

1. `ArquivosApp`
   - Disparado por eventos ocorridos na aplicação Arquivos App.

2. `WhatsApp`
   - Disparado por eventos ocorridos na aplicação WhatsApp/Meta.

3. `DialerApp`
   - Disparado por eventos ocorridos pelo Discador.

4. `GenericApp`
   - Disparado por requests genéricos contra nossa API com qualquer payload.
   - Para `GenericApp`, exigir no mínimo um objeto que contenha:

```json
{
  "external_id": "123456"
}
```

## Escopo da fase 1

Na primeira fase do projeto, precisamos ser capazes de:

1. Receber payloads no endpoint:

```http
POST /v1/orch/{flow_uuid}
```

2. Identificar automaticamente a origem/App do payload:
   - `ArquivosApp`
   - `WhatsApp`
   - `DialerApp`
   - `GenericApp`

3. Extrair os campos mínimos para controle de sessão:
   - `flow_uuid`
   - `entity`
   - `entity_type`
   - `entity_address`
   - `entity_session_id`
   - timestamps de eventos, quando existirem
   - variáveis de runtime em JSONB

4. Criar uma nova sessão quando não existir sessão ativa para a mesma combinação de:
   - `flow_uuid`
   - `entity`
   - `entity_type`
   - `entity_address`

5. Considerar como sessão ativa qualquer registro cujo `state` seja diferente de `3=finished`.

6. Se já existir sessão ativa para a mesma combinação, atualizar a sessão existente conforme o evento recebido.

7. Registrar/atualizar os campos específicos de status:
   - Dialer
   - WhatsApp
   - Futuramente SMS/RCS, embora não implementados nesta fase

8. Criar inicialmente apenas o script SQL de criação da tabela `orch_sessions`.

9. Depois que eu trouxer os dados de acesso e validar a tabela, avançaremos para a API FastAPI e os handlers de cada App.

## Estados de sessão

A coluna `state` seguirá este mapeamento:

```text
0 = pending   (default)
1 = running
2 = waiting
3 = finished
```

## Regra para início de nova sessão

O campo `started_at`, bem como a definição de início de uma nova sessão, se dará quando os valores presentes em:

- `flow_uuid`
- `entity`
- `entity_type`
- `entity_address`

não encontrarem registro na tabela com status diferente de `finished`.

Ou seja:

- Se não existir registro ativo para a combinação, criamos uma nova sessão.
- Se existir registro ativo, atualizamos a sessão existente.
- Registro ativo = `state <> 3`.

## Tabela principal

Criar a tabela:

```text
orch_sessions
```

Campos desejados:

```text
id
uuid
flow_uuid
state
entity
entity_type
entity_address
entity_session_id
started_at
ended_at
abandoned_at
frozen_until
last_card_uuid
next_card_uuid
runtime_variables
dialer_answered_at
dialer_busy_at
dialer_rejected_at
dialer_invalid_number_at
dialer_not_answered_at
dialer_failed_at
whatsapp_sent_at
whatsapp_delivered_at
whatsapp_read_at
whatsapp_failed_at
sms_sent_at
sms_failed_at
sms_delivered_at
rcs_sent_at
rcs_delivered_at
rcs_read_at
agent_interactions
```

### Significado dos principais campos

- `id`: identificador interno sequencial.
- `uuid`: UUID público da sessão.
- `flow_uuid`: UUID do fluxo recebido na URL.
- `state`: estado da sessão.
  - `0=pending`
  - `1=running`
  - `2=waiting`
  - `3=finished`
- `entity`:
  - Para `ArquivosApp`: `file.id`
  - Para `WhatsApp`: inicialmente usar identificador do contato/mensagem disponível no payload, preservando o bruto em `runtime_variables`
  - Para `DialerApp`: identificador relacionado à chamada/pessoa quando disponível (call-id)
  - Para `GenericApp`: `external_id`
- `entity_type`:
  - `file`
  - `person`
  - `api_request`
- `entity_address`:
  - Para `ArquivosApp`: `file.folder_path + "/" + file.original_name`
  - Para `DialerApp`: telefone extraído do payload, por exemplo de `CdrMailingData.phone` ou `DialString`
  - Para `WhatsApp`: telefone/wa_id/recipient_id disponível no evento
  - Para `GenericApp`: `external_id`
- `entity_session_id`:
  - Para `DialerApp`: call-id, por exemplo `uniqueid`, `Uniqueid` ou `Linkedid`
  - Para `ArquivosApp`: `file.id`
  - Para `WhatsApp`: `wa_id`
- `runtime_variables`: JSONB contendo as variáveis com valores reais extraídos e também o payload bruto ou dados relevantes para auditoria.
- `agent_interactions`: JSONB contendo variáveis ligadas ao resultado final após passagem no `finish_run`.

## Importante sobre payloads

Os payloads abaixo devem ser tratados como exemplos reais de entrada.

Não altere seus nomes de campos.

Não converta previamente os payloads para outro contrato.

Não exigir que terceiros mudem nada.

A aplicação deve ser flexível para detectar a origem e lidar com as diferenças.

---

# Payload exemplo — ArquivosApp

```json
{
  "EventName": "s3:ObjectCreated:Put",
  "Key": "files/workspace-ba7eb0ec-e565-447c-8c11-8f870cf72a60/xbank/enriquecimento/mailing_1_contato_adriano_oficial.csv",
  "Records": [
    {
      "awsRegion": "",
      "eventName": "s3:ObjectCreated:Put",
      "eventSource": "minio:s3",
      "eventTime": "2026-05-09T01:33:51.622Z",
      "eventVersion": "2.0",
      "requestParameters": {
        "principalId": "files-core-api",
        "region": "",
        "sourceIPAddress": "10.1.20.229"
      },
      "responseElements": {
        "x-amz-id-2": "dd9025bab4ad464b049177c95eb6ebf374d3b3fd1af9251148b658df7ac2e3e8",
        "x-amz-request-id": "18ADC1B9CC2407B2",
        "x-minio-deployment-id": "1716b3d9-30e9-43ee-ab1f-6205366327ae",
        "x-minio-origin-endpoint": "http://10.1.20.27:9000"
      },
      "s3": {
        "bucket": {
          "arn": "arn:aws:s3:::files",
          "name": "files",
          "ownerIdentity": {
            "principalId": "files-core-api"
          }
        },
        "configurationId": "Config",
        "object": {
          "contentType": "text/plain",
          "eTag": "256e4ae13654b1ca2151f969fd2925cd",
          "key": "workspace-ba7eb0ec-e565-447c-8c11-8f870cf72a60%2Fxbank%2Fenriquecimento%2Fmailing_1_contato_adriano_oficial.csv",
          "sequencer": "18ADC1B9CC2DE3EE",
          "size": 149,
          "userMetadata": {
            "content-type": "text/plain"
          }
        },
        "s3SchemaVersion": "1.0"
      },
      "source": {
        "host": "10.1.20.229",
        "port": "",
        "userAgent": "Boto3/1.42.69 md/Botocore#1.42.69 ua/2.1 os/linux#6.8.0-106-generic md/arch#x86_64 lang/python#3.12.13 md/pyimpl#CPython m/E,U,Z,G,b,N,e cfg/retry-mode#standard Botocore/1.42.69"
      },
      "userIdentity": {
        "principalId": "files-core-api"
      }
    }
  ],
  "file": {
    "created_at": "2026-05-09T01:33:51.635814Z",
    "folder_path": "xbank/enriquecimento",
    "id": "d5061f9a-0416-4719-886c-bfef5ff35696",
    "mime_type": "text/plain",
    "original_name": "mailing_1_contato_adriano_oficial.csv",
    "scan_status": "clean",
    "size_bytes": 149,
    "tags": [],
    "updated_at": "2026-05-09T01:33:51.635825Z",
    "url": "https://sync-core-api.otima.io/files/v1/files/content/d5061f9a-0416-4719-886c-bfef5ff35696",
    "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60"
  }
}
```

## Regras iniciais para ArquivosApp

Identificar como `ArquivosApp` quando o payload tiver sinais como:

- Campo `file`
- Campo `file.id`
- Campo `file.original_name`
- Campo `file.folder_path`
- Evento estilo S3/MinIO, como `EventName` ou `Records[].eventSource`

Mapeamento inicial:

```text
entity = file.id
entity_type = file
entity_address = file.folder_path + "/" + file.original_name
entity_session_id = file.id
started_at = timestamp atual ou file.created_at, se adequado
runtime_variables = JSONB com dados relevantes e payload bruto
```

---

# Payloads exemplo — WhatsApp

Importante: os exemplos abaixo representam todos os eventos a serem tratados nesta v1, mas servem apenas como modelo de observação/definição do padrão do payload.

Cada evento abaixo veio de uma chamada diferente.

Portanto, **não usar estes exemplos para inferir vínculo entre eles**.

Os eventos WhatsApp desta fase são:

- `sent`
- `delivered`
- `read`
- `failed`

## WhatsApp — sent

Payload observado no log:

```text
2026-05-08 08:15:33,280: INFO/MainProcess] webhook.whatsapp.forward.request url=https://sync-core-api.otima.io/whatsapp/provider/webhook headers={'Accept': 'application/json', 'Content-Type': 'application/json', 'X-APPLICATION-CLIENT-ID': '2cedc172-13ad-47f4-b714-2caca7953ffb', 'X-APPLICATION-CLIENT-SECRET': '***', 'X-WORKSPACE-UUID': 'bdbf27ec-0dd4-483a-aee5-78c9172e6cf4'} payload={'object': 'whatsapp_business_account', 'entry': [{'id': '751624234418652', 'changes': [{'value': {'messaging_product': 'whatsapp', 'metadata': {'display_phone_number': '5521983691497', 'phone_number_id': '756775060860079'}, 'contacts': [{'wa_id': '554196311412', 'user_id': 'BR.3949508488519420'}], 'statuses': [{'id': 'wamid.HBgMNTU0MTk2MzExNDEyFQIAERgSRUY5ODkxRkQyMDYzMEZGNkY2AA==', 'status': 'sent', 'timestamp': '1778238932', 'recipient_id': '554196311412', 'recipient_user_id': 'BR.3949508488519420', 'conversation': {'id': 'bed2de89093e953b76c68fc93131560b', 'expiration_timestamp': '1778238932', 'origin': {'type': 'utility'}}, 'pricing': {'billable': True, 'pricing_model': 'PMP', 'category': 'utility', 'type': 'regular'}}]}, 'field': 'messages'}]}], '_target_core': {'task_id': 'd40c9f49-5967-42b8-8617-b9f343a1e04d'}}
```

Payload em estrutura lógica:

```json
{
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "751624234418652",
      "changes": [
        {
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "5521983691497",
              "phone_number_id": "756775060860079"
            },
            "contacts": [
              {
                "wa_id": "554196311412",
                "user_id": "BR.3949508488519420"
              }
            ],
            "statuses": [
              {
                "id": "wamid.HBgMNTU0MTk2MzExNDEyFQIAERgSRUY5ODkxRkQyMDYzMEZGNkY2AA==",
                "status": "sent",
                "timestamp": "1778238932",
                "recipient_id": "554196311412",
                "recipient_user_id": "BR.3949508488519420",
                "conversation": {
                  "id": "bed2de89093e953b76c68fc93131560b",
                  "expiration_timestamp": "1778238932",
                  "origin": {
                    "type": "utility"
                  }
                },
                "pricing": {
                  "billable": true,
                  "pricing_model": "PMP",
                  "category": "utility",
                  "type": "regular"
                }
              }
            ]
          },
          "field": "messages"
        }
      ]
    }
  ],
  "_target_core": {
    "task_id": "d40c9f49-5967-42b8-8617-b9f343a1e04d"
  }
}
```

## WhatsApp — delivered

Payload observado no log:

```text
[2026-05-08 08:22:39,929: INFO/MainProcess] webhook.whatsapp.forward.request url=https://sync-core-api.otima.io/whatsapp/provider/webhook headers={'Accept': 'application/json', 'Content-Type': 'application/json', 'X-APPLICATION-CLIENT-ID': '2cedc172-13ad-47f4-b714-2caca7953ffb', 'X-APPLICATION-CLIENT-SECRET': '***', 'X-WORKSPACE-UUID': 'bdbf27ec-0dd4-483a-aee5-78c9172e6cf4'} payload={'object': 'whatsapp_business_account', 'entry': [{'id': '933542906048930', 'changes': [{'value': {'messaging_product': 'whatsapp', 'metadata': {'display_phone_number': '5511959330638', 'phone_number_id': '1098282763360750'}, 'contacts': [{'wa_id': '5521987930121', 'user_id': 'BR.2077296503193087'}], 'statuses': [{'id': 'wamid.HBgNNTUyMTk4NzkzMDEyMRUCABEYEjMyQzQ0NjQ3NkNGRUNFMEM2OAA=', 'status': 'delivered', 'timestamp': '1778239358', 'recipient_id': '5521987930121', 'recipient_user_id': 'BR.2077296503193087', 'biz_opaque_callback_data': '{"qj_contact_id": "431147737", "qj_deal_id": "58205237", "qj_creditor_id": "1", "qj_firing_id": "995513", "qj_stimulus_history_id": 2980778, "qj_sender_id": "18", "qj_initiative_id": "6966"}:::gen_txt_flows_agraciamossaldo_08042026', 'conversation': {'id': '224f9f2cb8c7b19b80aba593aa84bdad', 'origin': {'type': 'utility'}}, 'pricing': {'billable': True, 'pricing_model': 'PMP', 'category': 'utility', 'type': 'regular'}}]}, 'field': 'messages'}]}], '_target_core': {'task_id': 'd552aa44-41a1-4cb5-9135-de084cea22b0'}}
```

Payload em estrutura lógica:

```json
{
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "933542906048930",
      "changes": [
        {
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "5511959330638",
              "phone_number_id": "1098282763360750"
            },
            "contacts": [
              {
                "wa_id": "5521987930121",
                "user_id": "BR.2077296503193087"
              }
            ],
            "statuses": [
              {
                "id": "wamid.HBgNNTUyMTk4NzkzMDEyMRUCABEYEjMyQzQ0NjQ3NkNGRUNFMEM2OAA=",
                "status": "delivered",
                "timestamp": "1778239358",
                "recipient_id": "5521987930121",
                "recipient_user_id": "BR.2077296503193087",
                "biz_opaque_callback_data": "{\"qj_contact_id\": \"431147737\", \"qj_deal_id\": \"58205237\", \"qj_creditor_id\": \"1\", \"qj_firing_id\": \"995513\", \"qj_stimulus_history_id\": 2980778, \"qj_sender_id\": \"18\", \"qj_initiative_id\": \"6966\"}:::gen_txt_flows_agraciamossaldo_08042026",
                "conversation": {
                  "id": "224f9f2cb8c7b19b80aba593aa84bdad",
                  "origin": {
                    "type": "utility"
                  }
                },
                "pricing": {
                  "billable": true,
                  "pricing_model": "PMP",
                  "category": "utility",
                  "type": "regular"
                }
              }
            ]
          },
          "field": "messages"
        }
      ]
    }
  ],
  "_target_core": {
    "task_id": "d552aa44-41a1-4cb5-9135-de084cea22b0"
  }
}
```

## WhatsApp — read

Payload observado no log:

```text
[2026-05-08 08:39:11,241: INFO/MainProcess] webhook.whatsapp.forward.request url=https://sync-core-api.otima.io/whatsapp/provider/webhook headers={'Accept': 'application/json', 'Content-Type': 'application/json', 'X-APPLICATION-CLIENT-ID': '2cedc172-13ad-47f4-b714-2caca7953ffb', 'X-APPLICATION-CLIENT-SECRET': '***', 'X-WORKSPACE-UUID': 'bdbf27ec-0dd4-483a-aee5-78c9172e6cf4'} payload={'object': 'whatsapp_business_account', 'entry': [{'id': '1338767967847897', 'changes': [{'value': {'messaging_product': 'whatsapp', 'metadata': {'display_phone_number': '5511977310239', 'phone_number_id': '812230078638119'}, 'contacts': [{'wa_id': '5511982069650', 'user_id': 'BR.3828326980637572'}], 'statuses': [{'id': 'wamid.HBgNNTUxMTk4MjA2OTY1MBUCABEYEkQxOTE3Q0QzRTA3Q0M3REMxMAA=', 'status': 'read', 'timestamp': '1778240350', 'recipient_id': '5511982069650', 'recipient_user_id': 'BR.3828326980637572', 'conversation': {'id': 'd2fb17c228a9aca13c66151e51854786', 'origin': {'type': 'service'}}, 'pricing': {'billable': False, 'pricing_model': 'PMP', 'category': 'service', 'type': 'free_customer_service'}}]}, 'field': 'messages'}]}], '_target_core': {'task_id': '128ee847-0289-4a57-ac77-6b40acf0fc4d'}}
```

Payload em estrutura lógica:

```json
{
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "1338767967847897",
      "changes": [
        {
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "5511977310239",
              "phone_number_id": "812230078638119"
            },
            "contacts": [
              {
                "wa_id": "5511982069650",
                "user_id": "BR.3828326980637572"
              }
            ],
            "statuses": [
              {
                "id": "wamid.HBgNNTUxMTk4MjA2OTY1MBUCABEYEkQxOTE3Q0QzRTA3Q0M3REMxMAA=",
                "status": "read",
                "timestamp": "1778240350",
                "recipient_id": "5511982069650",
                "recipient_user_id": "BR.3828326980637572",
                "conversation": {
                  "id": "d2fb17c228a9aca13c66151e51854786",
                  "origin": {
                    "type": "service"
                  }
                },
                "pricing": {
                  "billable": false,
                  "pricing_model": "PMP",
                  "category": "service",
                  "type": "free_customer_service"
                }
              }
            ]
          },
          "field": "messages"
        }
      ]
    }
  ],
  "_target_core": {
    "task_id": "128ee847-0289-4a57-ac77-6b40acf0fc4d"
  }
}
```

## WhatsApp — failed

Payload observado no log:

```text
[2026-05-08 11:41:24,644: INFO/MainProcess] webhook.whatsapp.forward.request url=https://sync-core-api.otima.io/whatsapp/provider/webhook headers={'Accept': 'application/json', 'Content-Type': 'application/json', 'X-APPLICATION-CLIENT-ID': '2cedc172-13ad-47f4-b714-2caca7953ffb', 'X-APPLICATION-CLIENT-SECRET': '***', 'X-WORKSPACE-UUID': 'bdbf27ec-0dd4-483a-aee5-78c9172e6cf4'} payload={'object': 'whatsapp_business_account', 'entry': [{'id': '1202578251418504', 'changes': [{'value': {'messaging_product': 'whatsapp', 'metadata': {'display_phone_number': '5511962185237', 'phone_number_id': '967249473135513'}, 'contacts': [{'wa_id': '554192146473'}], 'statuses': [{'id': 'wamid.HBgMNTU0MTkyMTQ2NDczFQIAERgSOTU3MDkwOEFBNTNBRDE4MUFGAA==', 'status': 'failed', 'timestamp': '1778251283', 'recipient_id': '554192146473', 'errors': [{'code': 131026, 'title': 'Message undeliverable', 'message': 'Message undeliverable', 'error_data': {'details': 'Message Undeliverable.'}}]}]}, 'field': 'messages'}]}], '_target_core': {'task_id': '6c749432-1eb1-4a1f-af2f-fb2f64dfce98'}}
```

Payload em estrutura lógica:

```json
{
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "1202578251418504",
      "changes": [
        {
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "5511962185237",
              "phone_number_id": "967249473135513"
            },
            "contacts": [
              {
                "wa_id": "554192146473"
              }
            ],
            "statuses": [
              {
                "id": "wamid.HBgMNTU0MTkyMTQ2NDczFQIAERgSOTU3MDkwOEFBNTNBRDE4MUFGAA==",
                "status": "failed",
                "timestamp": "1778251283",
                "recipient_id": "554192146473",
                "errors": [
                  {
                    "code": 131026,
                    "title": "Message undeliverable",
                    "message": "Message undeliverable",
                    "error_data": {
                      "details": "Message Undeliverable."
                    }
                  }
                ]
              }
            ]
          },
          "field": "messages"
        }
      ]
    }
  ],
  "_target_core": {
    "task_id": "6c749432-1eb1-4a1f-af2f-fb2f64dfce98"
  }
}
```

## Regras iniciais para WhatsApp

Identificar como `WhatsApp` quando o payload tiver sinais como:

- `object = whatsapp_business_account`
- `entry[].changes[].value.messaging_product = whatsapp`
- `statuses[]`
- `contacts[].wa_id`

Mapeamento inicial sugerido:

```text
entity = contacts[0].wa_id ou statuses[0].recipient_id
entity_type = person
entity_address = contacts[0].wa_id ou statuses[0].recipient_id
entity_session_id = contacts[0].wa_id
```

Atualizar timestamps conforme `statuses[].status`:

```text
status = sent       -> whatsapp_sent_at = timestamp do evento
status = delivered  -> whatsapp_delivered_at = timestamp do evento
status = read       -> whatsapp_read_at = timestamp do evento
status = failed     -> whatsapp_failed_at = timestamp do evento
```

O campo `timestamp` do WhatsApp vem como Unix timestamp em string e deve ser convertido para timestamp no banco/aplicação quando possível.

Preservar o payload bruto e detalhes relevantes em `runtime_variables`.

---

# Payload exemplo — DialerApp

```json
{
  "hangup": {
    "AccountCode": "",
    "AnswerTime": "",
    "BillableSeconds": "0",
    "CallerIDName": "2a42709e-ef7e-4c56-822f-0fae421345d2",
    "CallerIDNum": "<unknown>",
    "Cause": "490",
    "Cause-txt": "kcpa_Silencio",
    "CdrMailingData": "{'phone': '5511975620806'}",
    "Channel": "SIP/trunk-sbc-router106-00000434",
    "ChannelState": "5",
    "ChannelStateDesc": "Ringing",
    "ConnectedLineName": "2a42709e-ef7e-4c56-822f-0fae421345d2",
    "ConnectedLineNum": "<unknown>",
    "Context": "default",
    "DialerActionID": "5efc17f8-1f36-4219-ac3c-8a28865dcb22",
    "DialerCampaignUUID": "2a42709e-ef7e-4c56-822f-0fae421345d2",
    "DialerClassifierStatus": "kcpa_Silencio",
    "DialerHangupCause": "490",
    "DialerWebhookCdrUrl": "['http://10.1.20.136:9701/v1/contact-supplier/0c8a4e83-fcff-4f0e-8459-9e6861449d59/517/sbc-feedback', 'https://api-bin.otima.io/webhook/327c57bc-09e2-4627-8ecd-11ab7dd002f8']",
    "DialerWorkspaceUUID": "0c8a4e83-fcff-4f0e-8459-9e6861449d59",
    "Disposition": "NO ANSWER",
    "Duration": "10",
    "EndTime": "2026-05-09 01:48:05",
    "Event": "Hangup",
    "Exten": "",
    "Language": "en",
    "LastData": "(Outgoing Line)",
    "Linkedid": "GW01-1778291275.2634",
    "PoolActionID": "5efc17f8-1f36-4219-ac3c-8a28865dcb22",
    "Priority": "1",
    "Privilege": "call,all",
    "StartTime": "2026-05-09 01:47:55",
    "SystemName": "GW01",
    "Uniqueid": "GW01-1778291275.2634",
    "content": ""
  },
  "makecall": {
    "DestAccountCode": "",
    "DestCallerIDName": "2a42709e-ef7e-4c56-822f-0fae421345d2",
    "DestCallerIDNum": "<unknown>",
    "DestChannel": "SIP/trunk-sbc-router106-00000434",
    "DestChannelState": "0",
    "DestChannelStateDesc": "Down",
    "DestConnectedLineName": "2a42709e-ef7e-4c56-822f-0fae421345d2",
    "DestConnectedLineNum": "<unknown>",
    "DestContext": "default",
    "DestExten": "",
    "DestLanguage": "en",
    "DestLinkedid": "GW01-1778291275.2634",
    "DestPriority": "1",
    "DestUniqueid": "GW01-1778291275.2634",
    "DialString": "trunk-sbc-router106/5511975620806",
    "Event": "DialBegin",
    "Privilege": "call,all",
    "SystemName": "GW01",
    "content": ""
  },
  "uniqueid": "GW01-1778291275.2634"
}
```

## Regras iniciais para DialerApp

Identificar como `DialerApp` quando o payload tiver sinais como:

- Campo `hangup`
- Campo `makecall`
- Campo `uniqueid`
- `hangup.Event = Hangup`
- `makecall.Event = DialBegin`
- Campos como `DialerActionID`, `DialerCampaignUUID`, `DialerWorkspaceUUID`, `Linkedid`, `Uniqueid`

Mapeamento inicial:

```text
entity = preferencialmente algum identificador de pessoa/ação se existir; na ausência, usar telefone
entity_type = person
entity_address = telefone
entity_session_id = uniqueid ou hangup.Uniqueid ou hangup.Linkedid
```

Para extrair telefone:

1. Tentar `hangup.CdrMailingData`, que vem como string parecida com dict Python:
   - Exemplo: `"{'phone': '5511975620806'}"`
2. Se não conseguir, tentar extrair de `makecall.DialString`
   - Exemplo: `trunk-sbc-router106/5511975620806`

Mapear eventos/timestamps:

- `hangup.StartTime` pode ajudar no `started_at`.
- `hangup.EndTime` pode ajudar no encerramento ou timestamp do resultado.
- `hangup.Disposition`, `hangup.Cause`, `hangup.Cause-txt`, `hangup.DialerClassifierStatus` e `hangup.DialerHangupCause` devem ser preservados em `runtime_variables`.

Mapeamento inicial de resultado Dialer (mesma DEF usada na outra aplicação) - trago aqui como exemplo:

```python
def map_release(code: Optional[int], *, protocol: Optional[str] = None, hint: Optional[str] = None) -> str:
    """
    Remapeia releases/codes (SIP ou ISUP/Q.850) para:
    \"busy\", \"noanswer\", \"machine\", \"invalidnumber\", \"rejected\", \"failure\", \"success\".
    """
    if hint:
        h = hint.upper()
        if any(k in h for k in ("MACHINE", "VOICEMAIL", "AMD", "BEEP", "SECRETARY", "FAX")):
            return "machine"

    if code is None:
        return "failure"

    if protocol is None:
        if code is not None and 100 <= int(code) <= 699:
            protocol = "sip"
        else:
            protocol = "isup"

    p = protocol.lower().strip()

    if p == "sip":
        if 200 <= code <= 299:
            return "success"
        if code in (486, 600):
            return "busy"
        if code in (480, 408, 487):
            return "noanswer"
        if code in (404, 484, 410, 604):
            return "invalidnumber"
        if code in (403, 401, 603):
            return "rejected"
        if 300 <= code <= 399:
            return "invalidnumber"
        return "failure"

    if p == "isup":
        if code == 16:
            return "success"
        if code == 17:
            return "busy"
        if code in (18, 19, 20, 31, 102):
            return "noanswer"
        if code in (1, 2, 3, 22, 26, 28):
            return "invalidnumber"
        if code in (21, 55, 57, 87):
            return "rejected"
        return "failure"

    return "failure"
```

Nesta fase, se houver dúvida sobre mapeamentos SIP/Q850/causas específicas, implemente de forma conservadora e preserve tudo em `runtime_variables`.

---

# GenericApp

O `GenericApp` será usado para qualquer payload genérico.

Regra mínima obrigatória:

```json
{
  "external_id": "123456"
}
```

Identificar como `GenericApp` quando:

- Não for possível classificar como `ArquivosApp`, `WhatsApp` ou `DialerApp`
- E o payload contiver `external_id`

Mapeamento:

```text
entity = external_id
entity_type = api_request
entity_address = external_id
entity_session_id = external_id
runtime_variables = payload bruto e campos úteis
```

Se o payload não for reconhecido e não tiver `external_id`, retornar erro `422 Unprocessable Entity` com mensagem clara em pt-BR.

---

# Script SQL solicitado para a fase 1

Nesta primeira etapa, eu criarei a tabela manualmente no PostgreSQL.

Portanto, nesta primeira entrega, gere um script SQL `CREATE TABLE` para a tabela `orch_sessions`.

Requisitos do SQL:

- Usar PostgreSQL.
- Criar extensão `pgcrypto` se necessário para `gen_random_uuid()`.
- `id` como `bigserial primary key`.
- `uuid` como `uuid not null default gen_random_uuid()`.
- `flow_uuid` como `uuid not null`.
- `state` como `smallint not null default 0`.
- `runtime_variables` como `jsonb not null default '{}'::jsonb`.
- `agent_interactions` como `jsonb not null default '{}'::jsonb`.
- Campos de data como `timestamptz`.
- Criar `created_at` e `updated_at` se achar importante para auditoria, mas não remover nenhum campo solicitado.
- Criar índices úteis para busca de sessão ativa:
  - índice por `flow_uuid`
  - índice por `entity`, `entity_type`, `entity_address`
  - índice parcial para sessões ativas onde `state <> 3`
  - índice por `entity_session_id`
  - índices para status/timestamps se fizer sentido
- Criar `CHECK` para `state in (0, 1, 2, 3)`.
- Não criar ainda tabelas extras sem necessidade.
- Se sugerir tabelas futuras, coloque apenas no README como evolução, sem implementar agora.

Campos obrigatórios da tabela:

```sql
id
uuid
flow_uuid
state
entity
entity_type
entity_address
entity_session_id
started_at
ended_at
abandoned_at
frozen_until
last_card_uuid
next_card_uuid
runtime_variables
dialer_answered_at
dialer_busy_at
dialer_rejected_at
dialer_invalid_number_at
dialer_not_answered_at
dialer_failed_at
whatsapp_sent_at
whatsapp_delivered_at
whatsapp_read_at
whatsapp_failed_at
sms_sent_at
sms_failed_at
sms_delivered_at
rcs_sent_at
rcs_delivered_at
rcs_read_at
agent_interactions
```

---

# README.md

Atualize/crie um `README.md` explicando:

1. O que é o projeto `orch`.
2. Objetivo da fase 1.
3. Stack utilizada.
4. Como ativar a `.venv`.
5. Como instalar dependências.
6. Como configurar `.env`.
7. Como executar a aplicação FastAPI futuramente.
8. Endpoint previsto:
   - `POST /v1/orch/{flow_uuid}`
9. Apps reconhecidas nesta fase:
   - `ArquivosApp`
   - `WhatsApp`
   - `DialerApp`
   - `GenericApp`
10. Regras de identificação de payload.
11. Regra de abertura/reuso de sessão.
12. Estrutura da tabela `orch_sessions`.
13. Campos ainda não implementados nesta fase:
   - SMS
   - RCS
   - RabbitMQ
   - Redis
14. Como vamos trabalhar:
   - sempre em pt-BR
   - pedir permissão quando precisar de elevação
   - usar `.venv`
   - preservar payloads externos
   - não alterar contratos de terceiros
15. Próximos passos da fase 1:
   - criar tabela manualmente
   - validar acesso ao banco via `.env`
   - implementar FastAPI
   - implementar detector de App
   - implementar handlers
   - implementar persistência de sessão
   - criar testes com os payloads acima

---

# Estrutura inicial sugerida do projeto

Se for criar estrutura de arquivos, use algo parecido com:

```text
orch/
  README.md
  .env.example
  requirements.txt ou pyproject.toml
  sql/
    001_create_orch_sessions.sql
  app/
    main.py
    core/
      config.py
      database.py
    api/
      v1/
        orch.py
    schemas/
      orch.py
    services/
      app_detector.py
      session_service.py
    handlers/
      arquivos_app.py
      whatsapp.py
      dialer_app.py
      generic_app.py
    repositories/
      orch_sessions_repository.py
    utils/
      datetime.py
      payload.py
  tests/
    payloads/
      arquivos_app.json
      whatsapp_sent.json
      whatsapp_delivered.json
      whatsapp_read.json
      whatsapp_failed.json
      dialer_app.json
      generic_app.json
```

Mas nesta primeira etapa, priorize:

1. `README.md`
2. `sql/001_create_orch_sessions.sql`
3. Um plano claro dos próximos passos

Não avance demais para implementação completa da API sem eu validar primeiro o SQL e a estrutura inicial.

---

# Critérios de aceite desta primeira entrega

A primeira resposta/alteração do projeto deve conter:

1. `README.md` criado ou atualizado.
2. Script SQL completo para criar `orch_sessions`.
3. Explicação em pt-BR do que foi criado.
4. Nenhuma alteração nos payloads externos.
5. Nenhuma implementação de RabbitMQ, Redis, SMS ou RCS nesta fase.
6. Nenhuma suposição que mude contrato de terceiros.
7. Projeto preparado para que, na próxima etapa, eu informe/valide os dados do PostgreSQL e então avancemos para a API FastAPI.

Comece pela criação/organização do README e do SQL da tabela `orch_sessions`.
