# Telemetria de Jornadas ORCH -> Metrics — contrato WebSocket superseded

> **SUPERSEDED em 2026-09-25.** Este contrato registra a fronteira que chegou
> a ser aceita com Metrics/SYNC, mas nunca recebeu writer ou publisher de
> runtime. O produto adotou WebSocket proprio do ORCH, sala e snapshot unicos
> por workspace e UI propria na Gestao de Extensoes. Nao implementar novos
> trabalhos a partir deste documento. A fonte vigente e
> `ORCHESTRATION_REPORTING_PLAN.md`; o novo contrato interno sera
> `ORCHESTRATION_WORKSPACE_WS_CONTRACT.md`.

## Estado historico

- Versao aceita: `0.2.0`.
- Data: 2026-09-25.
- Fonte duravel e produtora: ORCH.
- Consumidor de visualizacao: Metrics API/UI.
- Transporte: WebSocket SYNC ja usado por `dialer_metrics`.
- Contrato externo: somente `orchestration_journey_snapshot`.
- Retencao ORCH: configuravel por workspace entre 1 e 30 dias; padrao 30.
- Escopo historico: somente fatos posteriores ao `coverage_started_at` e ainda
  dentro da retencao efetiva.
- Backfill: inexistente.
- Gate 1: concluido em 2026-09-25 com as confirmacoes da Metrics/SYNC.
- Proximo gate: migrations e projecoes aditivas, ainda sem ativacao.

Este documento complementa `ORCHESTRATION_REPORTING_PLAN.md`. A versao
`0.1-draft` propunha fatos incrementais externos e persistencia pela Metrics.
Essa fronteira foi substituida: o ORCH guarda os fatos e a Metrics apenas
recebe uma projecao pronta para exibicao.

O contrato REST anterior permanece referencia historica de graos e canais,
mas nao e uma entrega ativa.

## Fronteira de responsabilidade

### ORCH

- persiste fatos normalizados de sessao, etapa, acionamento e evento de canal;
- aplica marco zero, retencao, limpeza e isolamento por workspace;
- mantem projecoes compactas e calcula snapshots deterministas;
- marca flows alterados como `dirty` e agrupa mudancas proximas;
- publica e republica o snapshot mais recente;
- conserva toda a verdade necessaria para reconstruir o snapshot enquanto os
  fatos estiverem dentro da retencao.

### Metrics/UI

- recebe snapshots prontos pelo SYNC;
- exibe o snapshot mais novo de cada workspace/flow/janela;
- nao precisa persistir fatos, reconstruir jornadas, tratar callbacks tardios
  ou deduplicar eventos internos;
- descarta snapshots antigos quando receber uma sequencia maior.

### SYNC

- autentica e transporta o envelope ate a Metrics;
- nao e fonte da verdade nem mecanismo de retencao;
- suas confirmacoes e limites servem para operacao do publisher, nao para
  integridade dos fatos.

O PDIAL nao calcula, agenda ou publica telemetria de jornada. O produtor e o
evento `dialer_metrics` permanecem independentes e inalterados.

## Persistencia interna e retencao

Os fatos internos nao sao mensagens WebSocket. O desenho minimo contempla:

- cobertura/configuracao do workspace;
- projecao compacta de cada sessao;
- visitas e transicoes de etapa;
- uma action por tentativa externa real;
- eventos normalizados ligados a action;
- estado de agregacao e entrega do snapshot por flow/janela.

Politica de retencao:

1. `retention_days` e configuravel por workspace entre `1` e `30`.
2. O valor padrao e `30`.
3. O limite absoluto de `30` deve existir em validacao e configuracao de
   seguranca; nenhum workspace o amplia silenciosamente.
4. Reduzir a retencao torna os fatos excedentes elegiveis para limpeza.
5. Aumentar a retencao nao recria dados ja removidos e nao executa backfill.
6. Limpeza ocorre em lotes pequenos, com indice temporal, sem bloquear o
   runtime funcional.
7. O marco `coverage_started_at` nunca e apagado pela limpeza.
8. O snapshot declara `oldest_available_at`, `retention_days` e eventuais
   lacunas em `data_quality`.
9. Callback tardio referente a action ja expirada nao recria historia; ele e
   descartado da telemetria com contador/alarme diagnostico, sem alterar a
   decisao funcional do canal.

Os relogios canonicos continuam separados:

- sessao: inicio efetivo da sessao;
- etapa: instante da entrada/saida registrada;
- action: instante da tentativa real;
- evento de canal: ocorrido, recebido e processado;
- snapshot: `as_of`, `generated_at` e `sent_at`.

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

O roteamento fisico termina na sala do usuario dentro do workspace. A
separacao por jornada e logica:

```text
topic = orchestration.journey.{workspace_uuid}.{flow_uuid}
```

Nao existe sala fisica por flow. O payload sempre declara o flow e a UI da
Metrics aplica o filtro em tela.

## Contrato externo unico

### `orchestration_journey_snapshot`

Estado agregado, reconstruivel e substituivel. Nao transporta os fatos internos
nem exige que a Metrics mantenha um event store.

```json
{
  "type": "orchestration_journey_snapshot",
  "topic": "orchestration.journey.<workspace_uuid>.<flow_uuid>",
  "meta": {
    "version": "0.2.0",
    "message_id": "<uuid>",
    "snapshot_id": "<uuid>",
    "snapshot_sequence": 42,
    "generated_at": "2026-09-25T12:00:04.900Z",
    "sent_at": "2026-09-25T12:00:05.000Z",
    "source_application": "orch"
  },
  "payload": {
    "as_of": "2026-09-25T12:00:04.900Z",
    "workspace": {"uuid": "<workspace_uuid>"},
    "flow": {"uuid": "<flow_uuid>", "name": "<flow_name>"},
    "retention": {
      "retention_days": 30,
      "maximum_days": 30,
      "coverage_started_at": "2026-09-25T11:00:00.000Z",
      "oldest_available_at": "2026-09-25T11:00:00.000Z"
    },
    "window": {
      "kind": "today",
      "from": "2026-09-25T03:00:00.000Z",
      "to": "2026-09-25T12:00:04.900Z",
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
    "time_series": [],
    "alerts": [],
    "data_quality": {}
  }
}
```

Cada snapshot representa uma janela declarada. A primeira entrega usa `today`
para acompanhamento operacional. Janelas adicionais de ate 30 dias somente
serao habilitadas depois de medir custo e tamanho. Para filtros de calendario,
o snapshot pode carregar buckets fechados em `time_series`; nao publica linhas
individuais de pessoa, address, mensagem ou sessao.

O contrato de uma eventual solicitacao sob demanda nao faz parte desta versao.
Sem contrato de request pelo SYNC, o ORCH publica apenas as janelas configuradas
e nao tenta antecipar combinacoes arbitrarias de filtros.

## Semantica de entrega

O objetivo e **latest-state delivery**, nao entrega duravel de cada mutacao:

1. uma gravacao interna marca o flow/janela como `dirty`;
2. mudancas proximas sao agrupadas por um debounce curto;
3. o worker constroi o snapshot a partir das projecoes duraveis;
4. `snapshot_sequence` cresce monotonicamente por workspace/flow/janela;
5. somente o snapshot mais novo precisa permanecer pendente;
6. nova mudanca substitui uma versao ainda nao enviada;
7. desconexao, restart ou heartbeat republicam o estado atual;
8. falha de WebSocket nunca bloqueia nem reverte a sessao funcional.

Nao existe outbox imutavel por evento externo. O estado de entrega conserva no
minimo `dirty_since`, versao construida, versao enviada, ultima tentativa,
proxima tentativa, erro e lease. O snapshot e sempre reconstruivel do banco.

`ws.send()` bem-sucedido comprova apenas aceite pelo socket local. Um ACK
correlacionado a `message_id`, se existir, melhora observabilidade; sua ausencia
nao causa perda de dados porque o snapshot atual e republicado periodicamente.

A Metrics deve substituir o snapshot corrente somente quando receber uma
`snapshot_sequence` maior para a mesma chave
`workspace + flow + window.kind + window.from + timezone`.

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

## Semantica dos blocos

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
cards. Cada sessao contribui com etapa atual e maior etapa. A agregacao declara
`basis=stage_rank`.

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

O grao interno e tentativa externa real:

- WhatsApp: uma mensagem outbound;
- SMS/RCS: um dispatch V2;
- Voz: uma tentativa de discagem;
- E-mail: `available=false` ate existir emissor homologado.

Cada canal informa contagens de actions, sessoes, pessoas pseudonimizadas,
estados nativos, marcos cumulativos, falhas e desconhecidos. Card visitado nao
cria action.

### `outcomes`

Colecao dinamica; nao codificar `acordo`, `recusa` ou `recado` globalmente.
Prioridade:

1. resultado explicito de `finish_flow`;
2. tabulacao/disposicao normalizada;
3. falha ou abandono;
4. `unknown`.

O status nativo e preservado internamente ao lado da categoria normalizada.

### `duration`

- media, mediana, P90 e P95 da sessao;
- faixas de duracao;
- opcionalmente permanencia por etapa;
- amostra e denominador obrigatorios.

### `time_series`

- buckets agregados dentro da janela;
- timezone e granularidade declarados;
- buckets semiabertos `[from, to)`;
- somente medidas aditivas podem ser somadas pela UI;
- percentis e medias de janelas diferentes nunca sao combinados pela UI sem
  numerador/amostra suficiente.

### `alerts`

- sem progresso alem do threshold;
- espera humana acima do threshold;
- erro tecnico terminal ou recorrente;
- repeticao anormal de identificacao/etapa;
- atraso de agregacao/publicacao;
- callback recebido depois da expiracao dos fatos relacionados.

## Privacidade e isolamento

- Nenhum texto de mensagem, prompt, corpo, token, segredo ou payload bruto.
- Nenhum address em claro.
- Snapshot agregado nao lista `person_uuid` nem `session_uuid`.
- Distinct count usa identificador pseudonimizado com sal por workspace apenas
  dentro do ORCH; o identificador nao precisa ser publicado.
- Toda mensagem declara workspace, flow e revisoes cobertas.
- Consumidor deve rejeitar divergencia entre `target_workspace_uuid` externo e
  `message.payload.workspace.uuid`.
- Dado de outro workspace nunca e aceito por fallback.

## Decisoes confirmadas pela Metrics/SYNC

1. O envelope `broadcast:dashboard`, `target_application=metrics` e
   `target_workspace_uuid` esta correto. A sala e por usuario; o filtro de
   flow e responsabilidade da UI sobre as dimensoes do payload.
2. O SYNC nao usa compressao e nao declara limite formal de payload. O ORCH
   ainda deve impor budget proprio, medir bytes e impedir snapshots sem limite
   operacional.
3. O limite informado e `400 mensagens/segundo`. O publisher deve operar
   abaixo do teto, coalescer mudancas e preservar margem para os demais
   produtores.
4. Nao existe ACK correlacionado a `message_id`. `ws.send()` sera registrado
   apenas como tentativa aceita pelo socket; reconexao e heartbeat republicam
   o snapshot atual.
5. A Metrics aceita `orchestration_journey_snapshot` e substitui o estado
   anterior quando recebe uma `snapshot_sequence` maior.

Consequencias operacionais:

- nao registrar status `delivered` para publicacao WebSocket;
- nao esperar ACK nem manter uma versao intermediaria apenas por sua ausencia;
- expor `last_socket_send_at`, erro, tentativa, dirty age e sequencias
  construida/enviada;
- manter snapshot por flow/janela mesmo que a sala seja por usuario;
- usar limite interno configuravel de payload e vazao antes do rollout, ainda
  que o SYNC nao imponha tamanho maximo formal.

O Gate 1 esta encerrado. O Gate 2 pode desenhar migrations e projecoes, mas
deve manter cobertura `pending`, flags desligadas, zero backfill e nenhuma
ativacao de workspace.
