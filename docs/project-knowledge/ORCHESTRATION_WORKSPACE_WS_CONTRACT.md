# Dashboard de Jornadas — contrato WebSocket por workspace

## Estado

- Versao: `1.0`.
- Data: 2026-09-25.
- Fonte duravel e agregador: ORCH.
- Consumidor inicial: UI de Gestao de Extensoes.
- Transporte: WebSocket proprio do ORCH.
- Escopo de publicacao: um snapshot logico por workspace.
- Retencao: configuravel entre 1 e 30 dias, padrao e teto 30.
- Backfill: inexistente.
- PDIAL e `dialer_metrics`: fora desta fronteira e inalterados.

Este documento complementa `ORCHESTRATION_REPORTING_PLAN.md`. O contrato
anterior com SYNC/Metrics esta preservado apenas como historico em
`ORCHESTRATION_JOURNEY_METRICS_WS_CONTRACT.md` e nao deve orientar o runtime.

## Invariantes

1. Sessoes e flows nunca abrem WebSocket nem publicam mensagens diretamente.
2. O runtime funcional somente grava fatos idempotentes e marca o workspace
   como `dirty` dentro de uma fronteira failure-safe.
3. Um lease duravel garante um unico agregador logico por workspace, mesmo com
   varios workers e hosts.
4. Mudancas de varios flows no mesmo debounce geram uma sequencia e um
   snapshot do workspace.
5. PostgreSQL guarda fatos, ultimo snapshot e sequencia; Redis e apenas plano
   de controle efemero para ticket de uso unico e anuncio
   `workspace_uuid + snapshot_sequence` entre replicas.
6. Perda de Redis, ausencia de cliente, desconexao ou cliente lento nunca
   perde fatos e nunca bloqueia uma sessao.
7. Ao conectar ou reconectar, qualquer replica recupera o ultimo snapshot do
   PostgreSQL antes de acompanhar novas notificacoes.
8. O payload nao contem pessoa, address, corpo de mensagem, payload bruto,
   token, credencial ou referencia externa reversivel.
9. A UI aceita somente sequencia maior que a ultima aplicada para o workspace.
10. O broadcast automatico contem overview e resumos compactos por flow;
    combinacoes detalhadas sao calculadas somente por request no mesmo socket.
11. Uma futura Metrics integra como cliente deste contrato; o ORCH nao volta a
    depender de sala, ACK ou API externa da Metrics.

## Topologia

```text
executor/callback ORCH
  -> fatos normalizados + workspace dirty
      -> Beat agenda apenas workspace UUID
          -> worker obtem lease e agrega
              -> PostgreSQL salva snapshot N
                  -> Redis publica {workspace_uuid, N}
                      -> replicas ORCH leem N e fazem fan-out local
                          -> UI aplica N se N > sequencia atual
```

Nao existe processo dedicado por workspace. Existe um pool de workers e uma
coordenacao duravel que permite somente um claim ativo por workspace.

## Autenticacao e sala

### Ticket

Endpoint chamado pelo navegador no BFF, sempre same-origin:

```text
POST /api/orchestration/journey-dashboard/ws-ticket
```

O BFF valida Basic Auth, CSRF e o workspace selecionado e chama, de forma
server-to-server, o endpoint interno do ORCH:

```text
POST /v1/orch/{workspace_uuid}/observability/journey-dashboard/ws-ticket
```

Ele reutiliza as credenciais de observabilidade server-side existentes; elas
nunca chegam ao bundle/navegador.

1. A UI autenticada solicita ao proprio BFF um ticket WebSocket.
2. O BFF autentica no ORCH por credencial server-to-server e informa o
   workspace ja autorizado pelo contexto da UI.
3. O ORCH cria um ticket opaco, de uso unico, com TTL inicial de 30 segundos,
   mantido no Redis com consumo atomico. Falha do Redis recusa novos tickets,
   mas nao afeta writers, fatos ou sessoes.
4. O ticket guarda no servidor: workspace, identidade tecnica do solicitante,
   instante de emissao, expiracao e nonce.
5. Ticket nao e JWT autocontido, nao carrega credencial e nao pode selecionar
   outro workspace.

Em sucesso, o BFF responde `201 Created` e nao ecoa o workspace:

```json
{
  "data": {
    "ticket": "<opaque-single-use-ticket>",
    "expires_in_seconds": 30
  }
}
```

Credencial ausente/invalida retorna `401`; workspace nao autorizado retorna
`404`; Redis indisponivel retorna `503`. A resposta nunca distingue workspace
inexistente de workspace fora do escopo do solicitante.

O endpoint HTTP de emissao do ticket e bootstrap de seguranca, nao transporte
de dados da dashboard.

### Handshake WebSocket

Endpoint same-origin aberto pelo navegador:

```text
/api/orchestration/journey-dashboard/ws
```

O BFF trata o HTTP `Upgrade`, valida a autenticacao HTTP Basic e encaminha o
tunel para o endpoint interno do ORCH, removendo `Authorization`, cookies e
headers de workspace originados no navegador:

```text
/v1/orch/observability/journey-dashboard/ws
```

O navegador abre o endpoint WebSocket sem segredo na URL. Em ate cinco
segundos, deve enviar:

```json
{
  "type": "authenticate",
  "version": "1.0",
  "ticket": "<opaque-single-use-ticket>"
}
```

Em sucesso, o ticket e consumido atomicamente e a conexao e vinculada a:

```text
orch.journey.workspace.<workspace_uuid>
```

Resposta:

```json
{
  "type": "authenticated",
  "version": "1.0",
  "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
  "heartbeat_seconds": 30
}
```

Falhas fecham o socket sem revelar se workspace ou ticket existem. Antes da
autenticacao, nenhuma inscricao Redis ou consulta de snapshot e permitida.

O BFF nao interpreta frames nem escolhe sala. O ticket e a unica autoridade
de workspace dentro do socket. Usar o BFF como proxy transparente preserva o
CSP `connect-src 'self'`, evita CORS, nao expoe o ORCH diretamente e permite
`ws://` no ambiente HTTP atual ou `wss://` quando a UI estiver sob HTTPS.

### Limites e codigos de fechamento

- mensagem de entrada: maximo de 16 KiB de JSON UTF-8;
- `request_id`: 1 a 64 caracteres opacos;
- uma conexao pertence a exatamente um workspace e um ticket abre somente uma
  conexao;
- limite inicial por replica: 64 conexoes por workspace e 512 no total;
- os limites sao guardrails por replica, nao quota comercial global;
- `1000`: encerramento normal;
- `1008`: mensagem ou transicao de protocolo invalida depois da autenticacao;
- `1009`: mensagem de entrada excedeu o limite;
- `1011`: erro interno nao recuperavel naquela conexao;
- `1013`: cliente lento ou capacidade temporariamente indisponivel; reconectar
  com jitter;
- `4401`: ticket ausente, expirado, reutilizado ou autenticacao nao concluida
  em cinco segundos;
- `4429`: limite de conexoes ou requisicoes excedido.

Falhas de ticket usam sempre `4401`, sem distinguir inexistente, expirado ou
ja consumido. Nenhum codigo revela a existencia de workspace.

## Tipos de mensagem do servidor

### `orchestration_workspace_snapshot`

Mensagem completa e substituivel. E a unica mensagem que altera a dashboard.

```json
{
  "type": "orchestration_workspace_snapshot",
  "meta": {
    "version": "1.0",
    "snapshot_id": "00000000-0000-0000-0000-000000000001",
    "snapshot_sequence": 42,
    "generated_at": "2026-09-25T21:00:04.900Z",
    "reason": "initial|update|heartbeat",
    "source_application": "orch"
  },
  "payload": {}
}
```

`reason` e observacional. Um heartbeat pode repetir a mesma sequencia; a UI
nao recalcula o estado quando a sequencia nao aumentou.

### `heartbeat`

Mantem a conexao e informa a maior sequencia conhecida sem carregar novamente
o snapshot:

```json
{
  "type": "heartbeat",
  "workspace_uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
  "latest_snapshot_sequence": 42,
  "sent_at": "2026-09-25T21:00:30.000Z"
}
```

Se a UI observar sequencia maior que a aplicada, envia:

```json
{"type": "resync"}
```

O servidor responde com o snapshot completo mais recente. Nao existe replay
de todas as sequencias intermediarias.

### `orchestration_view_request`

Solicita uma visao detalhada sem criar sala, worker ou publisher por flow. A
requisicao e aceita somente depois da autenticacao e sempre herda o workspace
da conexao:

```json
{
  "type": "orchestration_view_request",
  "request_id": "view-1",
  "filters": {
    "flow_uuid": "f77b70f0-849b-4d11-9ccc-449b3c4ba981",
    "revision_uuid": null,
    "channels": ["voice", "sms"],
    "period": {
      "from": "2026-09-25",
      "to": "2026-09-25",
      "timezone": "America/Sao_Paulo"
    }
  }
}
```

`request_id` e opaco, limitado e devolvido sem alteracao. Periodo deve estar
inteiramente dentro da cobertura e da retencao maxima de 30 dias.

### `orchestration_workspace_view`

Resposta correlacionada ao filtro solicitado:

```json
{
  "type": "orchestration_workspace_view",
  "request_id": "view-1",
  "snapshot_sequence": 42,
  "generated_at": "2026-09-25T21:00:05.100Z",
  "filters": {},
  "view": {}
}
```

A view e calculada exclusivamente sobre as projecoes novas. Ela pode ser
cacheada de forma limitada pela chave
`workspace + snapshot_sequence + filtros normalizados`; o cache e descartavel
e nunca e fonte da verdade. Quando o snapshot automatico avanca, a UI marca a
view atual como stale e solicita uma nova. Nao existe push automatico por flow.

### `error`

Erro de protocolo sanitizado. Erros internos ficam em log/alarme e nao vazam
SQL, stack trace, token ou detalhes de outro workspace.

```json
{
  "type": "error",
  "request_id": "view-1",
  "code": "invalid_filters",
  "message": "Os filtros informados nao sao validos.",
  "retryable": false
}
```

Codigos iniciais: `invalid_message`, `invalid_filters`, `period_unavailable`,
`view_busy`, `snapshot_unavailable` e `internal_error`. `request_id` somente e
devolvido quando era valido e estava presente na mensagem recebida.

## Payload do snapshot

O snapshot e agregado e autocontido para o overview do workspace. Nao e um
event stream nem transporta o cubo combinatorio de todos os filtros.

```json
{
  "as_of": "2026-09-25T21:00:04.900Z",
  "workspace": {
    "uuid": "ba7eb0ec-e565-447c-8c11-8f870cf72a60",
    "name": "Highcomm"
  },
  "retention": {
    "retention_days": 30,
    "maximum_days": 30,
    "coverage_started_at": "2026-09-25T18:00:00.000Z",
    "oldest_available_at": "2026-09-25T18:00:00.000Z"
  },
  "catalog": {
    "flows": [],
    "revisions": [],
    "channels": ["voice", "whatsapp", "sms", "rcs", "email"],
    "stages": []
  },
  "workspace_view": {},
  "flow_summaries": [],
  "data_quality": {
    "complete": true,
    "warnings": []
  }
}
```

### `workspace_view`, `flow_summaries` e views sob demanda

Cada view usa os mesmos blocos:

- `summary`: iniciadas, em andamento, concluidas, abandonadas, falhas,
  conversoes e sessoes sem acionamento;
- `active_progress`: distribuicao das sessoes ativas por etapa atual;
- `health`: `error`, `awaiting_intervention`, `stalled`, `delayed`, `normal`;
- `funnel`: as sete etapas canonicas, sempre presentes e ordenadas;
- `stage_dropoffs`: abandono por ultima etapa conhecida;
- `channels`: actions e resultados normalizados por canal;
- `outcomes`: desfechos terminais de sessao e de action com grao declarado;
- `duration`: media e histograma mergeavel; percentis da UI sao marcados como
  estimados quando derivados do histograma;
- `time_series`: buckets diarios da coorte de inicio e bucket operacional de
  hoje;
- `alerts`: contagens sanitizadas, nunca alarmes contendo entidade/address.

`flow_summaries` contem somente identidade, cobertura, lifecycle e contagens
compactas por flow. Funil, canais, desfechos, duracao e series filtrados ficam
na `orchestration_workspace_view`, criada apenas para a selecao ativa na UI.
Assim, cem flows nao multiplicam automaticamente 30 dias por sete etapas e
todos os resultados de canal em cada broadcast.

A view sob demanda permite as dimensoes data da coorte, flow, revisao, etapa,
canal e resultado normalizado. Contagens de sessao usam identidade distinta na
origem antes da agregacao, e cada linha declara `grain`.

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

Etapa vem exclusivamente do parametro `stage` do card na revisao fixada da
sessao. Saltos nao preenchem etapas, retorno nao reduz `highest_stage`, loops
incrementam visitas e uma etapa ausente gera warning de qualidade.

## Periodos e timezone

- Timezone inicial: `America/Sao_Paulo`, declarado em todos os buckets.
- Periodos: hoje, ontem, ultimos 7 dias, ultimos 30 dias e intervalo de datas
  inteiramente contido na cobertura/retencao.
- Sessoes usam coorte por `started_at`; actions usam `requested_at`; eventos
  preservam ocorrido, recebido e processado.
- O snapshot leva buckets diarios dos ultimos 30 dias e um bucket operacional
  de hoje. A UI recompõe intervalos somente com linhas de grao compativel.
- Aumentar retencao nao ressuscita dado removido; reduzir retencao torna fatos
  antigos elegiveis para limpeza incremental.

## Coalescencia, frequencia e backpressure

- Debounce inicial: 3 segundos por workspace, sujeito a medicao no canario.
- Uma nova mutacao durante agregacao deixa o workspace `dirty` para nova
  rodada; nao cancela nem mistura claims.
- Heartbeat: 30 segundos somente para conexoes autenticadas.
- Cada conexao mantem no maximo um snapshot automatico pendente; versao nova substitui a
  ainda nao enviada.
- Cada conexao aceita no maximo uma view em calculo e uma view pendente; nova
  solicitacao do mesmo cliente cancela logicamente a resposta anterior.
- Cliente persistentemente lento e encerrado com codigo retryable, podendo
  reconectar e obter o ultimo snapshot.
- `permessage-deflate` e permitido e deve ser negociado pelo servidor.
- Budget inicial a validar para o snapshot automatico: alvo de 256 KiB e teto
  de 1 MiB de JSON antes de compressao. Para view sob demanda: alvo de 256 KiB
  e teto de 1 MiB. Ultrapassar o teto nao trunca silenciosamente: conserva o
  ultimo estado valido, registra alarme e exige reduzir granularidade/buckets.

Requests de view acima de 16 KiB, periodos fora da cobertura, mais de cinco
canais, UUIDs invalidos ou filtros desconhecidos sao recusados sem consulta ao
banco. A UI aplica timeout de dez segundos a uma view; o servidor nao mantem
calculo abandonado como requisito de entrega.

## Sequencia e recuperacao

1. O worker obtem lease do workspace.
2. Constroi o snapshot a partir das projecoes novas; nunca varre
   `orch_session_metrics`.
3. Salva payload, bytes, hash e incrementa a sequencia na mesma transacao.
4. Depois do commit, publica apenas workspace e sequencia no Redis.
5. Cada replica busca a sequencia indicada, ignora notificacao antiga e faz
   fan-out aos clientes locais da sala.
6. Se Redis falhar, o snapshot continua no banco; heartbeat/reconexao o
   recuperam e o workspace permanece observavel como publish pending.
7. Nao existe ACK funcional do navegador. A conexao registra apenas ultimo
   frame aceito pelo socket; a UI sempre pode pedir `resync`.

## Qualidade e privacidade

O snapshot deve declarar, quando aplicavel:

- card executado sem `stage` valido;
- falha isolada de instrumentacao;
- callback tardio para action expirada;
- periodo parcialmente anterior a cobertura ou retencao;
- canal/provedor sem suporte a determinado evento;
- snapshot mantido por falha de rebuild ou payload acima do budget.

Nao transformar `unknown` em zero. Nao expor IDs de pessoa, address, corpo de
mensagem, payload de callback, provider reference sem hash ou dados de
credencial.

## Rollback

- Desabilitar criacao de tickets impede novos clientes sem afetar runtime.
- Desabilitar agregador interrompe snapshots, preservando fatos e dirty state.
- Desabilitar writers de telemetria interrompe coleta nova sem alterar cards.
- A UI mostra indisponibilidade/ultimo `as_of`; nunca inventa dado fresco.
- PDIAL, callbacks funcionais, Supplier e workflow nao dependem do gateway.

## Evidencias de fechamento documental do Gate 1

- o BFF Node 22 atual nao trata `Upgrade`; a extensao cirurgica sera feita no
  Gate 6, mantendo o caminho same-origin acima e sem dependencia nova;
- FastAPI/Starlette suporta endpoint WebSocket, JSON e fechamento por codigo;
- `ensure_active_workspace()` ja retorna `name` de `target.workspaces`; o Gate
  6 reutilizara esse valor uma vez durante o build, nunca por frame;
- fixture completa e limites sao validados por
  `tests/test_orchestration_workspace_ws_contract.py`;
- validacao integrada de proxy, ticket, compressao e browser pertence aos
  Gates 6 e 7 e nao reabre o contrato funcional aqui congelado.
