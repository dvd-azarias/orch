# Relatorios de Orquestracao — contrato funcional e de API

> **Historico/superseded em 2026-09-25.** Este contrato preserva o estudo da
> abordagem API/BFF/UI propria, mas nao deve ser implementado. A frente ativa e
> o contrato ORCH -> Metrics em
> `ORCHESTRATION_JOURNEY_METRICS_WS_CONTRACT.md`.

Contrato do Gate 2 do plano mestre
`ORCHESTRATION_REPORTING_PLAN.md`. Esta especificacao estende a observabilidade
existente; nao substitui o Rastreamento de Jornadas.

## Estado

- Versao do contrato: `v1`.
- Data: 2026-09-24.
- Escopo: leitura, um workspace e um flow por consulta.
- Autenticacao: a mesma credencial interna de observabilidade usada pelas
  rotas atuais; navegador acessa somente o BFF.
- O produto inicia em um marco zero por workspace. Nenhum dado anterior e
  exibido, copiado, inferido ou convertido.
- Exemplo serializado: `ORCHESTRATION_REPORTING_API_EXAMPLES.json`.

## Marco zero de cobertura

- a migration deixa o workspace em `pending`, sem liberar consultas;
- a ativacao operacional grava `coverage_started_at` uma unica vez, somente
  depois de todos os writers e callbacks passarem no smoke;
- requests com `from < coverage_started_at` recebem `422` com o instante
  minimo permitido;
- a UI desabilita datas anteriores e mostra `Dados disponiveis desde ...`;
- actions e eventos anteriores nao sao importados;
- callback sem uma action nova preexistente nao aparece neste relatorio;
- nao existe endpoint, task ou script de backfill.

## Graos e relogios

### Sessao

Uma execucao de flow. O periodo da lista e dos indicadores de sessao usa
`session_started_at = COALESCE(started_at, created_at)` e intervalo
semiaberto `[from, to)`.

`current_state` e um retrato atual e pode mudar depois da execucao, por
exemplo apos desvinculo de mailing. A API nao o apresenta como trilha
imutavel. Quando houver uma metrica terminal confiavel, `execution_result`
informa tambem `basis=terminal_metric`; caso contrario usa
`basis=current_snapshot`.

### Acionamento

Uma tentativa real de contato externo:

- WhatsApp: uma mensagem outbound aceita/identificada pelo provedor;
- SMS/RCS: um dispatch V2;
- voz: uma tentativa de discagem, nao um ciclo e nao uma visita ao card;
- e-mail: somente quando existir emissor real homologado.

O periodo dos indicadores de acionamento usa `requested_at` e `[from, to)`.
Callbacks tardios atualizam o mesmo acionamento e seus marcos, mas nao movem
o acionamento para outro bucket temporal.

### Evento

Uma mudanca de ciclo de vida associada ao acionamento. A ordenacao do detalhe
usa `occurred_at` quando informado e preserva `received_at` e `processed_at`
para diagnosticar atraso ou ordem de chegada.

### Visita ao card

E uma metrica de execucao do canvas. Nao e acionamento e nunca entra no total
de mensagens/chamadas. Continua disponivel no Rastreamento de Jornadas.

## Identidade e deduplicacao

Toda action possui:

- `action_id`: UUID estavel da projecao;
- `source_kind`: `whatsapp_outbound`, `supplier_v2_dispatch`,
  `supplier_v2_dial_attempt` ou futuro emissor homologado;
- `source_id`: identidade imutavel na fonte;
- `session_uuid`, `flow_uuid`, `flow_revision_id`;
- `component_ref_id`, `component_kind`, `channel` e `action_sequence`.

Unicidades obrigatorias:

1. `(source_kind, source_id)`;
2. `(session_uuid, flow_revision_id, component_ref_id, channel,
   action_sequence)` quando todos os campos forem conhecidos;
3. evento: `(action_id, provider_event_id, event_type)` quando houver ID;
4. sem ID de provedor, usar fingerprint deterministico do payload sanitizado
   e nunca o timestamp de recepcao isoladamente.

Callback repetido atualiza ou ignora a mesma identidade. Nunca cria outro
acionamento.

## Cobertura e qualidade

O relatorio so admite identidade completa e correlacao deterministica depois
do marco zero. `null` significa desconhecido ou nao suportado. Zero significa
que a fonte suporta o dado e nenhum item ocorreu. Toda resposta agrega:

```json
{
  "data_quality": {
    "coverage": "complete_since_cutover",
    "coverage_started_at": "2026-09-24T12:00:00Z",
    "actions": 12,
    "warnings": []
  }
}
```

Canal ainda nao homologado aparece em `channel_capabilities` como
`unavailable` e nao produz action. O valor nao e convertido em zero de envio.

## Estados comuns e estados nativos

`lifecycle_status` e operacional e mutualmente exclusivo:

- `prepared`;
- `queued`;
- `in_progress`;
- `accepted`;
- `completed`;
- `failed`;
- `uncertain`;
- `cancelled`;
- `unknown`.

Ele nao substitui `native_outcome`. Os marcos (`milestones`) sao cumulativos:
uma mensagem `read` tambem permanece contada como `sent` e `delivered` quando
esses eventos foram observados.

| Canal | Resultados/marcos nativos |
| --- | --- |
| WhatsApp | `sent`, `delivered`, `read`, `failed`, `response` |
| SMS | `accepted`, `sent`, `delivered`, `not_delivered`, `failed`, `rejected`, `expired`, `response`, `status` |
| RCS | `accepted`, `sent`, `delivered`, `read`, `unavailable`, `expired`, `failed`, `response`, `status` |
| Voz | `requested`, `dialing`, `answered`, `machine`, `busy`, `no_answer`, `rejected`, `invalid_number`, `technical_failure`, `cancelled` |
| E-mail | `unavailable` ate a homologacao do emissor; depois `accepted`, `sent`, `delivered`, `open`, `click`, `bounce`, `failed` conforme fonte real |

Resultados desconhecidos sao preservados em `provider_status` e expostos como
`native_outcome=unknown`; nao sao remapeados silenciosamente para falha.

## Indicadores e denominadores

Toda metrica retorna `value`, `unit` e `denominator` quando for taxa.

- `sessions_started`: sessoes iniciadas no periodo;
- `sessions_with_actions`: sessoes da coorte iniciada no periodo com ao menos
  um acionamento conhecido;
- `sessions_without_actions`: complemento comprovado da mesma coorte;
- `actions_requested`: acionamentos cujo `requested_at` esta no periodo;
- `unique_people`: pessoas distintas entre esses acionamentos;
- `provider_accepted`: actions com marco de aceite;
- `delivered_or_connected`: mensagem entregue ou voz atendida;
- `engaged`: WhatsApp/RCS lido/respondido, SMS respondido ou voz atendida;
- `failed`: actions com desfecho final de falha conhecido;
- `unknown`: actions sem desfecho suportado/conclusivo.

Taxas por canal usam como denominador apenas actions elegiveis para aquele
marco. E-mail indisponivel retorna `value=null`, nunca `0%`.

## Filtros

Comuns:

- `from` e `to`, ISO-8601 com timezone, intervalo `[from, to)`;
- `revision_id` opcional;
- `person_uuid` opcional;
- `address` opcional, comparacao normalizada exata;
- `channel` repetivel: `voice`, `whatsapp`, `sms`, `rcs`, `email`;
- `component_ref_id` repetivel;
- `lifecycle_status` repetivel;
- `native_outcome` repetivel;
- `session_state` repetivel na lista de sessoes;
- `has_actions=true|false` na lista de sessoes.

O flow permanece parte obrigatoria da rota. Autocomplete de flow e pessoa usa
as APIs ja existentes no BFF/Target Core; a API de relatorio nao cria uma
segunda busca cadastral.

## Periodo, timezone e buckets

- limite maximo: 744 horas (31 dias);
- default da UI: hoje no timezone selecionado;
- presets: hoje, ontem, 7 dias e 30 dias;
- timezone default da UI: `America/Sao_Paulo`, sempre enviado explicitamente;
- timestamps da API: ISO-8601 UTC;
- `bucket=auto`: hora para ate 48 horas; dia acima disso;
- `bucket=hour|day` pode ser solicitado dentro do limite;
- cada resposta inclui `as_of`, pois callbacks tardios podem completar marcos
  de uma coorte anterior.
- cada resposta inclui `coverage_started_at`; periodo anterior gera `422`, sem
  truncamento silencioso.

## Paginacao e ordenacao

- listas usam cursor opaco, nunca offset;
- `limit` default 50, minimo 1, maximo 100;
- actions: `requested_at DESC, action_id DESC`;
- sessions: `session_started_at DESC, session_id DESC`;
- eventos do detalhe: `occurred_at ASC, received_at ASC, event_id ASC`;
- cursor divergente dos filtros retorna `422`.

## PII e payloads

- `destination_display` sempre mascarado;
- `person_uuid` pode ser retornado ao leitor autenticado do workspace;
- provider message ID pode ser retornado para suporte;
- mensagem, token, credencial, envelope cifrado, callback assinado e payload
  bruto do provedor nunca sao retornados;
- `metadata` aceita somente chaves sanitizadas previstas em allowlist;
- nenhum endpoint permite escolher schema fora do workspace autenticado.

## Rotas

Todas estendem o namespace atual:

```text
GET /v1/orch/{workspace_uuid}/observability/flows/{flow_uuid}/reports/summary
GET /v1/orch/{workspace_uuid}/observability/flows/{flow_uuid}/reports/timeseries
GET /v1/orch/{workspace_uuid}/observability/flows/{flow_uuid}/reports/sessions
GET /v1/orch/{workspace_uuid}/observability/flows/{flow_uuid}/reports/actions
GET /v1/orch/{workspace_uuid}/observability/flows/{flow_uuid}/reports/channels
GET /v1/orch/{workspace_uuid}/observability/flows/{flow_uuid}/reports/actions/{action_id}
```

### `summary`

Retorna coorte de sessoes, volume de actions, pessoas, funil cumulativo,
distribuicao por canal/desfecho e qualidade. Nao retorna linhas individuais.

### `timeseries`

Parametros adicionais: `metric=sessions_started|actions_requested` e
`bucket=auto|hour|day`. Cada ponto pode informar canal.

### `sessions`

Uma linha por sessao e contagem derivada de actions, canais distintos e ultimo
acionamento. Inclui link logico para a rota de trace existente, sem duplicar o
canvas no contrato.

### `actions`

Uma linha por tentativa real, com marcos, desfecho nativo e correlacao. Nao
inclui a lista completa de eventos.

### `channels`

Agrega por canal, card e resultados nativos. `milestone_counts` e cumulativo;
`final_outcome_counts` e mutualmente exclusivo.

### `actions/{action_id}`

Retorna a action, sua timeline de eventos sanitizados e uma referencia para a
sessao/trace. `action_id` de outro workspace ou flow responde `404`.

## Envelope comum

```json
{
  "api_version": "v1",
  "workspace_uuid": "uuid",
  "flow_uuid": "uuid",
  "period": {
    "from": "2026-09-24T12:00:00Z",
    "to": "2026-09-25T00:00:00Z",
    "timezone": "America/Sao_Paulo",
    "time_basis": "action_requested_at"
  },
  "as_of": "2026-09-24T12:00:00Z",
  "data_quality": {
    "coverage": "complete_since_cutover",
    "coverage_started_at": "2026-09-24T12:00:00Z",
    "actions": 1,
    "warnings": []
  },
  "data": {}
}
```

Listas adicionam `items` e `page.next_cursor`. Erros seguem o padrao atual de
observabilidade: `401`, `404`, `422`, `503` por timeout seguro e `500` somente
para falha nao classificada.

## Regras de consulta e seguranca

1. A primeira instrucao SQL da requisicao permanece `SET TRANSACTION READ
   ONLY`.
2. Validar workspace ativo antes de configurar `search_path`.
3. `statement_timeout` permanece fail-closed e configuravel.
4. Nao usar `orch_session_metrics` como fonte primaria de actions.
5. Consultas de detalhe nunca retornam payload bruto.
6. Resumos de 30 dias dependem dos indices/projecao do Gate 3; nao liberar a
   rota fazendo agregacao integral sobre metricas antigas.
7. O BFF adiciona apenas estes GETs a sua allowlist.

## Casos de aceite do contrato

1. Sessao sem action: `action_count=0`, `channels=[]`.
2. Uma mensagem com `sent/delivered/read`: uma action, tres marcos, nao tres
   actions.
3. Duas mensagens do mesmo card: duas actions com sequencias distintas.
4. Discador com tres tentativas no mesmo ciclo: tres actions de voz.
5. Callback duplicado: totais inalterados.
6. Callback fora de ordem: timeline preserva ocorrido/recebido e marcos
   cumulativos coerentes.
7. Periodo anterior a `coverage_started_at`: `422`, sem dados parciais.
8. Callback antigo sem action nova: nao aparece e nao cria action.
9. E-mail marker-only: zero action de envio e capacidade `unavailable`.
10. Sessao posteriormente desvinculada: `current_state` atual sem apagar as
    actions registradas pelo novo ledger.
11. Flow/revisao/card repetidos em outro workspace: isolamento integral.
