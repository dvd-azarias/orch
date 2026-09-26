# Dashboard de Jornadas de Orquestracao — plano mestre

Plano iniciado em 2026-09-23 para uma visao analitica de sessoes,
acionamentos e eventos de canal. Em 2026-09-25, depois de congelar o primeiro
contrato externo, o produto foi redirecionado novamente: o ORCH permanece
como fonte duravel, mas passa tambem a agregar e servir um snapshot unico por
workspace por WebSocket proprio. A UI oficial desta frente sera construida na
Gestao de Extensoes. SYNC e Metrics deixam de ser dependencias; no futuro, a
Metrics podera integrar como cliente do contrato publicado pelo ORCH.

Este documento e a fonte unica da verdade desta frente. Toda retomada deve
comecar pela leitura de **Status atual**, **Checkpoint de retorno** e do
primeiro gate ainda aberto. Nao considerar um gate concluido sem registrar a
evidencia correspondente neste arquivo.

## Status atual

- **Frente ativa:** Gate 7 — integracao e validacao da UI oficial.
- **Ultimo gate concluido localmente:** Gate 6 — projecoes, agregador, fila,
  snapshot duravel e gateway WebSocket proprio. Latencia sob carga e prova
  entre replicas continuam como criterios de Gate 8/rollout, nao como sucesso
  presumido.
- **Classificacao:** `ALPHA_FIX_OPTIONAL`, com beneficio operacional direto
  para suporte e diagnostico. Mudancas de runtime devem permanecer pequenas,
  aditivas e protegidas.
- **Escopo autorizado:** persistencia/projecoes minimas, retencao configuravel
  de 1 a 30 dias, instrumentacao idempotente, dirty state por workspace,
  agregador e fila exclusivos, gateway WebSocket proprio, snapshot agregado,
  UI na Gestao de Extensoes, homologacao canaria e rollout controlado.
- **Fora do escopo:** backfill, atribuicao comercial, custos, BI externo,
  dependencia de SYNC/Metrics e grandes refatoracoes do runtime.
- **Marco zero aprovado:** a telemetria considera somente sessoes,
  acionamentos e eventos produzidos depois da ativacao explicita do novo
  mecanismo em cada workspace. Nao existe backfill, importacao, inferencia ou
  exibicao de dados anteriores.

## Redirecionamento vigente aprovado em 2026-09-25

1. O estudo dos Gates historicos permanece valido para graos,
   denominadores, canais, idempotencia, privacidade e marco zero.
2. O ORCH guarda fatos incrementais, agrega e publica um unico contrato por
   workspace: `orchestration_workspace_snapshot`.
3. O transporte SYNC e a UI da Metrics ficam formalmente substituidos nesta
   frente. A Metrics podera futuramente escutar o WebSocket do ORCH sem
   alterar a fonte duravel nem a semantica dos fatos.
4. O evento `dialer_metrics`, seu produtor PDIAL e seus consumidores nao sao
   alterados.
5. O Beat apenas agenda workspaces `dirty`. Persistencia de fatos, agregacao,
   fan-out e UI pertencem a fronteiras dedicadas; indisponibilidade da
   dashboard nunca pode impedir o andamento das sessoes.
6. O gateway mantem uma sala logica por workspace:
   `orch.journey.workspace.{workspace_uuid}`. Flow e dimensao do snapshot e
   filtro local da UI; nao existe emissor automatico por flow.
7. `orch_session_metrics` nao sera varrida para produzir snapshots. As
   projecoes novas existem justamente para evitar agregacao repetida sobre
   dezenas de milhoes de linhas.
8. A retencao efetiva e configuravel por workspace entre 1 e 30 dias, com
   padrao e teto absoluto de 30. Aumentar o periodo nao recria dados removidos.
9. O transporte adota `latest-state delivery`: mudancas proximas de quaisquer
   flows sao agrupadas, somente o snapshot mais novo do workspace permanece
   pendente e reconexao ou heartbeat republicam o estado atual.
10. O ORCH define autenticacao, compressao, payload budget, sequencia,
    heartbeat e backpressure do proprio WebSocket. PostgreSQL e fonte da
    verdade; Redis serve apenas a notificacao/fan-out entre replicas.

## Funil canonico de jornada

Todo snapshot apresenta, nesta ordem, exatamente as sete etapas:

1. `entrada`;
2. `identificacao`;
3. `qualificacao`;
4. `abordagem`;
5. `proposta`;
6. `decisao`;
7. `desfecho`.

Regras:

- a etapa vem do parametro `stage` do card na revisao fixada da sessao;
- uma sessao conta uma vez em `reached_sessions` de cada etapa;
- loops incrementam visitas e transicoes, sem duplicar a sessao no funil;
- retorno a uma etapa anterior nao reduz `highest_stage`;
- saltos entre etapas sao preservados e nao preenchem etapas inexistentes;
- etapa sem ocorrencia aparece com zero e com sua cobertura declarada;
- chegar a `desfecho` nao implica conversao;
- conversao exige resultado terminal explicitamente classificado como
  sucesso;
- conclusao improdutiva e concluida, mas nao convertida;
- abandono significa termino anormal sem `finish_flow`, nao simplesmente
  ausencia de conversao.

## Checkpoint de retorno obrigatorio

Esta frente e uma pausa controlada, nao uma troca do objetivo principal.

- Workspace HighComm: `ba7eb0ec-e565-447c-8c11-8f870cf72a60`.
- Flow de retorno imediato:
  `f77b70f0-849b-4d11-9ccc-449b3c4ba981`.
- Ultima evidencia consolidada antes da pausa:
  - sessao `8561`;
  - UUID `c9bdbd81-fe05-4d60-9938-5ba790241ae7`;
  - WhatsApp enviado em `2026-09-23 21:56:11` BRT;
  - entregue em `2026-09-23 21:56:12` BRT;
  - lido em `2026-09-23 21:56:25` BRT;
  - sessao finalizada em `state=3`.
- Observacao posterior: depois do desvinculo do mailing, a mesma sessao passou
  a aparecer em `state=5`. Isso nao invalida a conclusao anterior, mas prova
  que `orch_sessions.state` representa o estado corrente e nao deve ser usado
  isoladamente como historico imutavel do desfecho.
- Ao concluir os relatorios, retomar a homologacao do flow exatamente depois
  dessa comprovacao, sem reiniciar o desenho nem repetir gates ja fechados.
- O objetivo estrategico maior continua sendo tornar o fluxo completo
  funcional e homologado; esta tela deve passar a ser usada como instrumento
  de verificacao dos proximos cards e canais.

## Workspace e flow de ensaio

### HighComm — canario controlado

- Workspace: `ba7eb0ec-e565-447c-8c11-8f870cf72a60`.
- Flow: `f77b70f0-849b-4d11-9ccc-449b3c4ba981`.
- Uso: gerar cenarios conhecidos de zero, um e varios acionamentos, inclusive
  repeticao do mesmo canal, multiplos canais, falha, timeout e callbacks
  duplicados ou fora de ordem.
- Depois da homologacao, cada workspace de producao recebe seu proprio marco
  de ativacao. Dados anteriores ao marco permanecem intactos, mas invisiveis
  para o novo relatorio.

## Marco zero e regra de corte

1. A migration cria a estrutura em estado `pending`; isso nao inicia cobertura.
2. Depois de API, workers e integracoes estarem na mesma versao e passarem no
   smoke, um comando administrativo ativa o relatorio naquele workspace.
3. A ativacao grava `coverage_started_at` uma unica vez e e auditavel.
4. Nenhum snapshot inclui periodo anterior ao maior valor entre
   `coverage_started_at` e `oldest_available_at`; nao corta periodo
   silenciosamente.
5. Cada snapshot declara permanentemente `coverage_started_at`,
   `oldest_available_at`, `retention_days` e avisos de qualidade.
6. Callback nao cria acionamento. Ele somente atualiza uma action previamente
   registrada pelo novo mecanismo; callback antigo sem action fica fora do
   relatorio.
7. Nao ha script, task, migration ou endpoint de backfill.

## Semantica congelada

```text
Sessao de orquestracao
  -> zero ou muitos acionamentos
       -> zero ou muitos eventos de provedor
```

1. **Sessao** e uma instancia de execucao do flow.
2. **Acionamento** e uma tentativa real de contato externo originada por um
   card de canal; em voz, cada tentativa de discagem e um acionamento, mesmo
   quando varias pertencem ao mesmo ciclo/card.
3. **Evento** e uma mudanca de ciclo de vida ou retorno do provedor associado
   a um acionamento.
4. Uma sessao sem canal deve permanecer consultavel como `sem acionamento`.
5. Reentrar no mesmo card ou repetir o mesmo canal cria outro acionamento; nao
   cria outra sessao.
6. Callback repetido nao cria outro acionamento nem aumenta contagens.
7. Toda metrica deve declarar seu grao: sessoes, pessoas, acionamentos ou
   eventos. A UI nao pode misturar esses denominadores.
8. Revisao do flow e `component_ref_id` sao dimensoes obrigatorias; o mesmo
   flow pode mudar de desenho e conter mais de um card do mesmo canal.
9. `sent` nao significa `delivered`; `delivered` nao significa `read` ou
   `answered`.
10. Ausencia de suporte do provedor deve aparecer como `nao informado`, nunca
    como zero.
11. Dados anteriores ao marco zero nao pertencem ao produto de Relatorios e
    nunca sao inferidos a partir de metricas, timestamps ou callbacks antigos.

## Contrato visual da nossa dashboard

A imagem `ORCHoficialDASHBOARD.png` e a referencia funcional. A UI sera
construida e mantida no repositorio oficial `GOHP-LAB/target-extensions-ui` e
consumira exclusivamente o WebSocket proprio do ORCH para os dados da
dashboard. O snapshot deve fornecer dados suficientes para os seguintes
  filtros. O overview e a lista compacta de flows chegam no snapshot automatico;
  filtros detalhados usam request/response no mesmo WebSocket, somente enquanto
  houver usuario interessado:

- workspace selecionado globalmente;
- flow com autocomplete por nome;
- periodo: hoje, ontem, 7 dias, 30 dias ou intervalo customizado;
- revisao do flow;
- estado da sessao;
- canal, com multi-selecao;
- card/componente;
- resultado do acionamento;
- sessoes com acionamento, sem acionamento ou todas.

### Visao geral

- `sessions_started`: sessoes distintas iniciadas no periodo;
- `sessions_in_progress`: sessoes da coorte ainda nao terminais em `as_of`;
- `sessions_completed`: sessoes concluidas por `finish_flow`;
- `sessions_abandoned`: sessoes terminalizadas sem conclusao normal;
- `conversion`: conclusoes explicitamente classificadas como sucesso e seu
  denominador;
- `sessions_stalled`: sessoes ativas sem progresso alem do limite configurado;
- duracao media, mediana, P90, P95 e faixas de duracao;
- progresso das sessoes ativas com `basis=stage_rank`, sem fingir que um grafo
  com branches e loops e linear;
- saude mutualmente exclusiva, na precedencia `error`,
  `awaiting_intervention`, `stalled`, `delayed`, `normal`;
- funil canonico de sete etapas, transicoes e abandono por etapa;
- distribuicao por canal, card e desfecho;
- alertas de travamento, espera humana, erro tecnico e repeticao anormal;
- `coverage_started_at`, `as_of` e avisos de qualidade sempre presentes.

### Visao por canal

| Canal | Ciclo e resultados desejados |
| --- | --- |
| WhatsApp | `sent`, `delivered`, `read`, `failed`, resposta/interacao quando disponivel |
| SMS | `sent`, `delivered`, `failed`, `rejected`, `expired` quando informado |
| RCS | `sent`, `delivered`, `read`, `failed`, clique/interacao/resposta |
| Voz | solicitado, discado, `answered`, `machine`, `busy`, `no_answer`, `rejected`, `invalid_number`, `failure` |
| E-mail | `sent`, `delivered`, `open`, `click`, `bounce`, `failed`, somente conforme o ciclo for implementado |

## Fronteira tecnica aprovada

```text
Runtime ORCH
  -> fatos/projecoes com retencao no schema do workspace
      -> dirty state + fila/worker exclusivo por workspace
          -> snapshot persistido do workspace
              -> notificacao Redis entre replicas
                  -> gateway WebSocket proprio do ORCH
                      -> UI de Gestao de Extensoes
```

- A UI nao acessa diretamente o banco do ORCH.
- Credenciais e tickets WebSocket permanecem fora do Git.
- Fatos incrementais permanecem internos ao ORCH; somente snapshots agregados
  atravessam o WebSocket.
- O envio automatico e `latest-state`: a UI substitui o snapshot pela maior sequencia
  monotonica do mesmo workspace.
- Ausencia temporaria de consumidores preserva os fatos, o snapshot mais
  recente e o estado `dirty`, sem bloquear a sessao nem reter cada versao
  intermediaria.
- O navegador obtem um ticket curto e de uso unico pelo BFF; depois do
  handshake, snapshot inicial, atualizacoes e heartbeat trafegam somente pelo
  WebSocket.
- Detalhe por flow/revisao/canal/periodo nao cria publisher nem sala propria. A
  UI envia uma requisicao no socket do workspace e recebe uma view correlacionada
  por `request_id` e `snapshot_sequence`.
- Em ambiente com varias replicas, apenas um worker agrega um workspace por
  lease. Cada API entrega a notificacao Redis somente aos sockets locais; ao
  reconectar, qualquer replica recupera o ultimo snapshot no PostgreSQL.
- Nenhum corpo de mensagem, token, credencial ou payload bruto e publicado.
- O PDIAL e o evento `dialer_metrics` permanecem contratos independentes.

## Persistencia aprovada para detalhamento no Gate 2

O levantamento deve primeiro determinar a cobertura real de:

- `orch_sessions` — ciclo da sessao e timestamps resumidos;
- `orch_session_metrics` — passagens repetidas pelos cards;
- `orch_channel_events` — ledger de callbacks de canal.

Essas fontes nao correlacionam de maneira deterministica varias ocorrencias do
mesmo card/canal na mesma sessao e nao devem ser varridas repetidamente para
alimentar a dashboard. O desenho aditivo a detalhar e validar por migration
compreende:

- `orch_journey_flow_coverage`: cobertura e marco zero por flow;
- `orch_journey_sessions`: projecao compacta de estado, etapa atual/mais alta,
  ultimo progresso e resultado terminal;
- `orch_journey_stage_visits`: primeira/ultima entrada, visitas, permanencia e
  transicoes por sessao/revisao/etapa;
- `orch_journey_channel_actions`: uma tentativa externa real por linha;
- `orch_journey_channel_action_events`: eventos normalizados ligados a action;
- novo controle por workspace/janela: `dirty_since`, ultimo snapshot,
  sequencia, lease, tentativas, proxima tentativa e ultimo erro.

`orch_journey_snapshot_delivery` foi criado pela migration `0023` para a
fronteira anterior por flow/SYNC. Ele nao possui writer nem dados funcionais e
permanecera inerte; uma migration aditiva criara o controle correto por
workspace sem reescrever migration ja aplicada.

O ledger `orch_journey_channel_actions` conserva:

- `action_id` idempotente;
- sessao, flow e revisao;
- `component_ref_id`, tipo do componente e ordinal da visita/tentativa;
- canal, pessoa e destino mascaravel;
- estado corrente/final e desfecho;
- identificadores externos do provedor;
- timestamps do ciclo de vida;
- metadados sanitizados;
- indices por workspace/flow/tempo, sessao, canal e estado.

Uma tabela de controle registra `coverage_started_at`. Somente actions criadas
pelo runtime novo depois desse instante entram na telemetria. As fontes atuais
foram auditadas apenas para desenhar a fronteira; elas nao serao importadas.

A mesma tabela registra `retention_days`, validado entre 1 e 30. A limpeza
remove fatos expirados em lotes pequenos e ordem referencial, preserva o marco
zero e nunca executa backfill. Reduzir a retencao torna dados antigos elegiveis
para limpeza; aumentar a retencao vale somente para fatos ainda existentes.

Uma falha de gravacao de telemetria deve usar savepoint, registrar alarme e
preservar o comportamento funcional da sessao. O modo ativo nao pode esconder
lacunas: snapshots carregam `data_quality.warnings` quando houver falha de
instrumentacao ou cobertura incompleta de `stage`.

## Evidencias da auditoria historica — 2026-09-24

Levantamento executado somente em leitura no banco compartilhado, sem
`EXPLAIN ANALYZE`, replay, alteracao de flow, escrita funcional ou reinicio de
servico. Nenhuma linha auditada sera importada para o novo relatorio.

### Inventario confirmado

| Fonte | Grao real | O que prova | Limite confirmado |
| --- | --- | --- | --- |
| `orch_sessions` | uma linha por sessao | identidade, flow, estado corrente, cursor e timestamps resumidos | um unico timestamp por tipo; nao representa varias mensagens/chamadas e o estado pode mudar depois do encerramento |
| `orch_session_metrics` | uma linha por execucao/reexecucao de card ou workflow | passagem pelo card, revisao, `component_ref_id`, latencia e motivo de parada | callback e retomada reexecutam o card; linha de metrica nao equivale a acionamento |
| `orch_channel_events` | uma linha deduplicada por sessao/canal/evento/status | callbacks recebidos, tempo do provedor, tempo de recepcao e descarte | dados anteriores nao garantem `component_ref_id` nem uma identidade uniforme de acionamento e ficam fora do novo relatorio |
| `contact_supplier_channel_dispatches_v2` | um dispatch SMS/RCS | sessao, revisao, card, sequencia, aceite/falha e ID do provedor | nao substitui o ledger de callbacks e cobre somente SMS/RCS V2 |
| `contact_supplier_dial_cycles_v2` | um ciclo criado pelo card de discador | sessao, revisao, card, perfil e decisao terminal | um ciclo pode produzir zero, uma ou varias tentativas reais |
| `contact_supplier_dial_attempts_v2` | uma tentativa real de voz | ordinal, IDs do discador/PBX, telefone, inicio e desfecho | esta e a fonte correta do acionamento de voz; contar o ciclo ou o card seria incorreto |
| `contact_supplier_dial_events_v2` | um evento de telefonia de uma tentativa | release bruto, resultado normalizado, consumo de tentativa e decisao | fonte especifica da Supplier V2; precisa ser projetada para o contrato comum |

### Prova de que passagem pelo card nao e acionamento

Na sessao HighComm `8561` (`c9bdbd81-fe05-4d60-9938-5ba790241ae7`):

- uma unica mensagem WhatsApp real gerou cinco metricas do mesmo card:
  preparacao, `sent`, `delivered`, `read` e resposta;
- uma unica tentativa real de voz gerou duas metricas do card: preparacao e
  retomada pelo callback;
- o SMS real gerou um dispatch V2 com ID
  `f5c2daac-41fb-447a-bba9-7fe8e6608da8`, estado `accepted`, uma tentativa de
  envio e ID de mensagem do provedor;
- o discador gerou ciclo `dea9639a-f1d1-4000-a7d5-cc93a517ac55`, tentativa
  `67213058-f637-4d58-9837-8d011483f264` e evento terminal
  `technical_failure` normalizado de `CONGESTION`;
- o aceite do SMS existe na Supplier V2, mas nao apareceu nos timestamps
  resumidos de SMS da sessao. Portanto, esses timestamps nao podem ser a fonte
  canonica do relatorio.

O levantamento confirmou tambem que sessoes, metricas e callbacks possuem
relogios distintos. Por isso o contrato novo define explicitamente o relogio
de cada grao e nao tenta reconstruir actions antigas por proximidade temporal.

### Volume e planos de consulta

| Workspace | Tabela | Linhas atuais | Tamanho total aproximado |
| --- | --- | ---: | ---: |
| HighComm | `orch_sessions` | 2.450 | 13,3 MB |
| HighComm | `orch_session_metrics` | 27.194.654 | 13,30 GB |
| HighComm | `orch_channel_events` | 1.393 | 1,97 MB |
Um workspace de producao auditado ja possuia aproximadamente 88 milhoes de
metricas e 43 GB incluindo indices. Esses numeros servem somente como prova de
risco operacional; os registros nao serao expostos nem copiados.

Planos somente estimados confirmaram:

- `orch_session_metrics` usa `idx_orch_session_metrics_flow_created` para
  flow e periodo;
- a lista de sessoes existente pode filtrar o flow depois do indice de data,
  pois nao existe indice composto `flow_uuid + started_at`;
- a agregacao existente de eventos pode fazer `Seq Scan`, pois nao existe
  indice composto `flow_uuid + created_at`;
- mesmo com indice, recalcular relatorios sobre dezenas de milhoes de metricas
  por abertura de tela e um desenho operacionalmente inadequado.

### Matriz de cobertura por canal

| Canal | Inicio/acionamento | Eventos e desfechos | Correlacao atual | Cobertura |
| --- | --- | --- | --- | --- |
| WhatsApp | criar action no outbound novo antes/ao confirmar o envio | `sent`, `delivered`, `read`, `failed` e inbound vinculados a `action_id` | obrigatoria por action, mensagem do provedor e card | **obrigatoria a partir do marco zero** |
| SMS V2 | `contact_supplier_channel_dispatches_v2`, com card, revisao, sequencia e ID do provedor | normalizador ORCH suporta `sent`, `delivered`, `not_delivered`, `failed`, status e resposta; callbacks novos carregam `dispatch_identity` | deterministica por sessao, revisao, card, canal e `dispatch_sequence` | **confirmada** para dispatch; callback real ainda nao observado no canario auditado |
| RCS V2 | mesma tabela de dispatch SMS/RCS | normalizador suporta `sent`, `delivered`, `read`, `unavailable`, `expired`, `failed`, status e resposta | deterministica pela mesma `dispatch_identity` | **confirmada por contrato e testes**, mas sem amostra real neste recorte |
| Voz V2 | cada linha de `contact_supplier_dial_attempts_v2` e uma chamada real | `contact_supplier_dial_events_v2` guarda release bruto, normalizado, contagem e decisao | deterministica por ciclo, tentativa, card, IDs do discador e PBX | **confirmada** no canario |
| E-mail | somente preparacao/marcacao; envio e ciclo de eventos nao homologados | inexistente | inexistente | **indisponivel**; nao pode aparecer como enviado |

### Duplicidade, atraso e ordem

- `orch_channel_events` deduplica o mesmo status do mesmo evento no escopo da
  sessao, mas permite que o mesmo ID do provedor evolua por `sent`,
  `delivered` e `read`.
- `event_ts`, `received_at` e `processed_at` permitem mostrar evento ocorrido,
  recebido e processado sem reordenacao silenciosa.
- callbacks SMS/RCS V2 incluem a identidade do dispatch no payload sanitizado
  e callbacks tardios sao preservados com descarte explicito, em vez de
  reabrirem a sessao.
- eventos crus da voz V2 no ledger generico podem aparecer descartados porque
  a decisao canonica vive nas tabelas da Supplier V2. Eles nao devem ser
  contados duas vezes.

### Decisao da auditoria historica

**CONFIRMED:** um ledger/projecao uniforme de acionamentos e necessario.

O menor desenho seguro e `orch_channel_actions` como projecao operacional
idempotente, sem substituir as fontes especializadas:

- uma linha por tentativa real de contato externo, nao por visita ao card;
- voz: uma action por `contact_supplier_dial_attempts_v2`, nunca por ciclo;
- SMS/RCS: uma action por dispatch V2;
- WhatsApp: uma action por mensagem outbound/ID de provedor;
- e-mail: nenhuma action de envio enquanto seu emissor nao existir;
- chave da fonte (`source_kind`, `source_id`) e identidade funcional
  (`session`, revisao, card, canal, sequencia) unicas;
- callbacks atualizam/anexam eventos de forma idempotente e nunca criam um
  novo envio por repeticao;
- nenhuma fonte anterior a `coverage_started_at` e consultada, copiada ou
  inferida.

A etapa ativa de persistencia deve decidir se a projecao sera escrita
sincronicamente nas fronteiras ja duraveis ou alimentada por tarefas
idempotentes. Nao deve consultar ou agregar `orch_session_metrics` como fonte
primaria de acionamentos.

## Gates ativos — dashboard propria por workspace

### Gate 0 — redirecionamento, memoria e retorno

- [x] Preservar os estudos de grao, canais e idempotencia ja confirmados.
- [x] Substituir SYNC/Metrics por WebSocket e UI proprios nesta frente.
- [x] Definir um emissor logico e uma sala por workspace, nunca por sessao ou
  flow.
- [x] Congelar as sete etapas canonicas do funil.
- [x] Preservar marco zero, ausencia de backfill e retorno obrigatorio ao
  canario e ao fluxo completo.
- [x] Confirmar que fatos/projecoes da migration `0023` permanecem validos e
  que apenas o controle de entrega por flow sera aposentado antes de uso.
- [x] Nao alterar runtime, banco implantado, PDIAL ou flow neste gate.

**Resultado:** concluido documentalmente em 2026-09-25. O antigo contrato
`ORCHESTRATION_JOURNEY_METRICS_WS_CONTRACT.md` permanece como historico
superseded; nao e backlog nem contrato de runtime.

### Gate 1 — contrato do WebSocket proprio

- [x] Congelar `orchestration_workspace_snapshot` e todos os blocos da imagem.
- [x] Definir sala `orch.journey.workspace.{workspace_uuid}` e autorizacao
  fail-closed por workspace.
- [x] Definir ticket curto/de uso unico, handshake, heartbeat, reconexao e
  encerramento de conexao.
- [x] Definir `snapshot_sequence` monotonica por workspace e descarte de
  mensagens antigas na UI.
- [x] Definir snapshots completos substituiveis; nao transmitir fatos internos
  ou eventos por sessao.
- [x] Definir buckets que permitam hoje, ontem, 7 dias, 30 dias e intervalo de
  datas sem nova emissao por flow.
- [x] Definir payload budget, compressao, debounce, timeout e backpressure.
- [x] Congelar timezone, coortes, denominadores, desfechos, conversao,
  abandono, saude e limites de atraso/travamento.
- [x] Congelar retencao entre 1 e 30 dias e callback posterior a expiracao.
- [x] Criar fixtures do snapshot e da view sob demanda; validar overview local
  e filtros detalhados pelo mesmo socket.

**Criterio de saida:** contrato interno versionado sem ambiguidade de grao,
periodo, sala, autenticacao ou idempotencia.

**Resultado:** concluido documentalmente em 2026-09-25 por
`ORCHESTRATION_WORKSPACE_WS_CONTRACT.md`, fixture JSON e testes de contrato. A
UI abre um socket same-origin no BFF; o BFF apenas autentica o Upgrade e cria o
tunel para o ORCH. O broadcast automatico ficou deliberadamente compacto por
workspace, e a visao detalhada e requisitada no mesmo socket para impedir um
cubo combinatorio de flows, dias, etapas e canais. A prova integrada do tunel,
ticket e browser permanece nos Gates 6 e 7, onde existe runtime para testa-la.

### Gate 2 — persistencia e controle por workspace

- [x] Migration `0023` criou configuracao, cobertura por flow, projecao de
  sessoes, visitas de etapa, actions e eventos normalizados.
- [x] Retencao 1–30 dias, constraints, unicidades e indices foram validados.
- [x] A migration foi aplicada somente no HighComm e os objetos/defaults foram
  confirmados antes de qualquer writer.
- [x] Criar migration aditiva para dirty state, lease e ultimo snapshot por
  workspace/janela.
- [x] Manter `orch_journey_snapshot_delivery` sem writer e documentada como
  obsoleta; remove-la somente em limpeza futura explicitamente aprovada.
- [x] Implementar limpeza incremental sem backfill.
- [x] Provar idempotencia/rollback sem tocar tabelas funcionais.

**Criterio de saida:** uma unica coordenacao duravel por workspace, estruturas
reversiveis e nenhum comportamento de sessao alterado.

**Resultado:** concluido localmente em 2026-09-25 pela migration aditiva
`0024`, ainda nao aplicada a workspace real. O singleton usa geracoes para nao
perder dirty state durante o build, lease expiravel, retry e ultimo snapshot
com teto estrutural de 1 MiB. A limpeza remove em lotes somente sessoes
terminais expiradas e deixa sessoes ativas antigas intactas. Migration,
coalescencia concorrente, retry e limpeza passaram em schemas PostgreSQL
temporarios com rollback; nenhum hook de runtime foi conectado neste gate.

### Gate 3 — instrumentacao de sessao e etapas

- [x] Registrar inicio, entrada/transicao de etapa, espera, retomada e termino.
- [x] Resolver `stage` exclusivamente na revisao fixada da sessao.
- [x] Tornar eventos idempotentes em reexecucoes e callbacks repetidos.
- [x] Preservar loops, retornos e saltos sem inventar etapas.
- [x] Calcular etapa atual, maior etapa e numero de visitas separadamente.
- [x] Classificar cobertura ausente de `stage` como warning, nunca como zero
  silencioso.
- [x] Fazer falha de telemetria usar savepoint/alarme sem interromper o fluxo.

**Criterio de saida:** timeline exata de etapas para sessoes controladas, sem
mudanca funcional no workflow.

### Gate 4 — actions, canais e desfechos

- [x] WhatsApp: uma action por mensagem outbound real.
- [x] SMS/RCS V2: uma action por dispatch, atualizada pelos callbacks.
- [x] Voz V2: uma action por tentativa real, nunca por ciclo/card.
- [x] Manter e-mail indisponivel enquanto nao houver emissor homologado.
- [x] Normalizar resultados sem apagar o status nativo do provedor.
- [x] Deduplicar callback e preservar ocorrido, recebido e processado.
- [x] Definir resultado terminal por `finish_flow`/tabulacao explicita, sem
  inferir sucesso apenas por chegar a `desfecho`.

**Criterio de saida:** zero, um e varios acionamentos por sessao reconciliam
exatamente com as fontes especializadas.

### Gate 5 — agregador unico por workspace

- [x] Criar fila exclusiva conforme `ORCH_QUEUE_PROFILE`.
- [x] Criar worker com hostname explicito e sem reutilizar fila existente.
- [x] Fazer o Beat apenas enfileirar workspaces sujos; nenhuma agregacao pesada no
  Beat.
- [x] Manter o schedule desligado por default e ativa-lo explicitamente em um
  unico Beat somente depois de migration/escopo canario.
- [x] Implementar debounce/coalescencia, lease, `SKIP LOCKED`, retry/backoff e
  `latest-state delivery`.
- [x] Colapsar alteracoes de varios flows no mesmo debounce em um snapshot do
  workspace.
- [x] Preservar fatos, dirty state e ultimo snapshot quando nao houver UI
  conectada, sem acumular versoes intermediarias.
- [x] Adicionar logs estruturados de versao, tentativa, falha e notificacao.
- [x] Manter o produtor e contrato `dialer_metrics` intocados.

**Criterio de saida:** o estado mais novo sobrevive a desconexao e reinicio,
um unico worker logico agrega cada workspace e nada bloqueia sessoes.

### Gate 6 — gateway WebSocket proprio e snapshot

- [x] Gerar resumo, progresso ativo, saude, funil, abandono por etapa, canais,
  desfechos, duracao e alertas.
- [x] Gerar somente sobre projecoes novas; proibir varredura de
  `orch_session_metrics`.
- [x] Publicar `as_of`, `coverage_started_at`, denominadores e qualidade.
- [x] Publicar `retention_days`, `oldest_available_at` e janela inteiramente
  contida na cobertura ainda retida.
- [x] Usar sequencia monotonica por workspace/janela.
- [x] Criar gateway com ticket curto e sala rigidamente vinculada ao workspace.
- [x] Usar Redis apenas para notificar replicas; PostgreSQL permanece fonte da
  verdade e fornece o snapshot inicial/reconexao.
- [x] Configurar debounce de tres segundos e heartbeat em trinta segundos;
  a comprovacao do SLO de cinco segundos permanece no Gate 8.
- [ ] Medir plano/latencia e impor budget por snapshot.
- [x] Manter todos os dados da dashboard no WebSocket; HTTP serve apenas ao
  bootstrap seguro do ticket.

**Criterio de saida:** snapshot deterministico e reconciliavel com os fatos,
recebido por clientes em replicas distintas e dentro do budget operacional.

### Gate 7 — UI propria na Gestao de Extensoes

- [x] Partir do `main` atualizado de `GOHP-LAB/target-extensions-ui`.
- [x] Criar Dashboard em Orquestracao, preservando o rastreamento de sessoes
  existente como drill-down complementar.
- [x] Implementar visao geral, funil, flows, canais, desfechos, duracao, serie
  temporal, saude, alertas e qualidade da cobertura.
- [x] Implementar filtros locais por flow, revisao, periodo e canal.
- [x] Exibir conexao, reconexao, snapshot sequence, cobertura, vazio, parcial e
  indisponivel sem mascarar lacunas.
- [ ] Validar responsividade, navegacao por teclado e degradacao segura no
  browser contra runtime local.

**Criterio de saida:** a tela da imagem e reproduzida com dados exclusivamente
do WebSocket proprio, sem banco no navegador e sem dependencia de SYNC.

### Gate 8 — testes locais e adversariais

- [ ] Fato interno duplicado, atrasado e fora de ordem.
- [ ] Reexecucao do mesmo card e loop entre etapas.
- [ ] Salto de etapa e revisoes diferentes do mesmo flow.
- [ ] Falha de agregacao, Redis, WebSocket, ticket e reconexao entre replicas.
- [ ] Reinicio do worker durante envio.
- [ ] Varios flows alterados no mesmo debounce produzem uma emissao do
  workspace.
- [ ] Retencao 1/30 dias, reducao, aumento sem ressurreicao e limpeza em lotes.
- [ ] Isolamento entre workspace/flow e ausencia de PII/payload bruto.
- [ ] Regressao completa do workflow e stacks anteriores ativas.
- [ ] Repetir `SUBA_O_AMBIENTE` e smoke encadeado apos mudanca de runtime.

**Criterio de saida:** testes automatizados e runtime local provam recuperacao,
idempotencia e ausencia de regressao funcional.

### Gate 9 — homologacao HighComm

- [ ] Aplicar migration sem ativar cobertura.
- [ ] Ativar somente workspace `ba7eb0ec-e565-447c-8c11-8f870cf72a60` e flow
  `f77b70f0-849b-4d11-9ccc-449b3c4ba981` depois do smoke.
- [ ] Confirmar que o snapshot sempre traz as sete etapas.
- [ ] Reconciliar as quatro etapas atualmente usadas pelo canario e os tres
  zeros legitimos (`entrada`, `proposta`, `decisao`).
- [ ] Caso sejam necessarios valores nao zero nas sete etapas, usar revisao
  controlada ou clone de telemetria; nao reclassificar cards falsamente.
- [ ] Provar zero, um e varios canais, loop, sucesso, falha e espera.
- [ ] Reconciliar fatos internos, snapshot WebSocket e nossa UI.

**Criterio de saida:** contagens exatas e tela reconciliada no workspace
HighComm, incluindo reconexao e atualizacao ao vivo.

### Gate 10 — rollout, handoff futuro e retorno

- [ ] Comparar contagens controladas e medir atraso, dirty age, payload, P95 e
  republicacao apos desconexao.
- [ ] Validar carga, P95, isolamento, mascaramento e rollback.
- [ ] Confirmar regressao do PDIAL e de `dialer_metrics` sem mudanca.
- [ ] Expandir somente por allowlist de workspace/flow.
- [ ] Entregar contrato e exemplos finais da nossa UI; uma futura integracao
  Metrics deve consumir este contrato e nao inverter a dependencia.
- [ ] Atualizar arquitetura, banco, configuracao, runbook, riscos e memoria.
- [ ] Registrar commits, deploys e evidencias no `MAINTENANCE_LOG.md`.
- [ ] Marcar pendencias objetivas sem prolongar esta pausa.
- [ ] Retomar a homologacao do flow
  `f77b70f0-849b-4d11-9ccc-449b3c4ba981` no checkpoint registrado.
- [ ] Retomar em seguida o objetivo estrategico do fluxo completo.

**Criterio de saida:** dashboard propria estavel e auditavel sob carga
representativa, rollback comprovado e trabalho principal retomado no ponto
registrado.

## Gates historicos — preservados como evidencia, substituidos em 2026-09-25

Os Gates abaixo documentam o estudo original de API/BFF/UI. Gates 0 a 2
continuam sendo evidencia tecnica; Gates 3 a 10 nao constituem mais backlog e
foram substituidos pelos Gates ativos acima.

### Gate 0 — controle, memoria e retorno

- [x] Persistir o plano mestre e seu checklist.
- [x] Registrar o checkpoint exato do flow HighComm antes da pausa.
- [x] Registrar os dois workspaces e flows de ensaio.
- [x] Preservar a retomada do fluxo completo como saida obrigatoria.
- [x] Nao alterar runtime, banco ou dados funcionais neste gate.

**Evidencia:** este documento e a entrada correspondente no
`PROJECT_BRAIN.md`. Nenhum deploy, migration ou mutacao de flow faz parte do
Gate 0.

### Gate 1 — auditoria somente leitura e matriz de cobertura

- [x] Inventariar fontes atuais de sessao, card, acionamento e callback.
- [x] Mapear, por canal, os eventos persistidos e suas chaves de correlacao.
- [x] Provar como loops e varias ocorrencias do mesmo card aparecem hoje.
- [x] Identificar callbacks duplicados, atrasados e fora de ordem.
- [x] Provar que as fontes anteriores nao devem alimentar o novo relatorio.
- [x] Medir volume e plano de consulta antes de propor endpoints.
- [x] Produzir matriz `evento esperado x fonte x correlacao x cobertura`.
- [x] Decidir com evidencia se `orch_channel_actions` e necessario.

**Criterio de saida:** matriz de cobertura registrada neste documento e gaps
classificados como confirmado ou indisponivel para instrumentacao futura.

**Resultado:** criterio cumprido em 2026-09-24. Auditoria executada em leitura;
nenhuma mutation, replay ou `EXPLAIN ANALYZE` foi usado. Por decisao posterior
do produto, os dados auditados servem somente como evidencia arquitetural e
nao farao parte do relatorio.

### Gate 2 — contrato funcional e de API

- [x] Congelar graos, denominadores e regras de deduplicacao.
- [x] Congelar estados nativos de WhatsApp, SMS, RCS, voz e e-mail.
- [x] Definir JSON de filtros, agregados, linhas e drill-down.
- [x] Definir paginacao, timezone, limites de periodo e ordenacao.
- [x] Definir mascaramento de PII e autorizacao por workspace.
- [x] Definir representacao de dado desconhecido ou nao suportado.
- [x] Validar exemplos de zero, um e varios acionamentos.

**Criterio de saida:** contrato revisavel, com fixtures de resposta e sem
ambiguidade entre sessao, acionamento e evento.

**Resultado:** criterio cumprido em 2026-09-24 por
`ORCHESTRATION_REPORTING_API_CONTRACT.md` e
`ORCHESTRATION_REPORTING_API_EXAMPLES.json`. O contrato usa tentativa externa
real como action, marcos cumulativos, marco zero e cursores opacos.

### Gate 3 — persistencia minima, se necessaria

- [ ] Confirmar que o Gate 1 exige nova persistencia.
- [ ] Desenhar migration aditiva e rollback seguro.
- [ ] Criar controle `pending|active` com `coverage_started_at` imutavel.
- [ ] Definir identidade/idempotencia do acionamento.
- [ ] Instrumentar somente as fronteiras de cards/callbacks necessarias.
- [ ] Criar indices orientados pelas consultas do Gate 1.
- [ ] Preservar comportamento dos cards e callbacks existentes.
- [ ] Garantir por teste que registros anteriores ao marco zero nao aparecem.
- [ ] Nao criar mecanismo de backfill, importacao ou inferencia retroativa.

**Criterio de saida:** multiplos acionamentos e eventos correlacionados sem
duplicar efeito nem modificar semantica de runtime.

### Gate 4 — APIs read-only do ORCH

- [ ] Implementar resumo e serie temporal.
- [ ] Implementar lista de sessoes.
- [ ] Implementar lista e detalhe de acionamentos.
- [ ] Implementar agregacao por canal/card/desfecho.
- [ ] Aplicar filtros e paginacao no servidor.
- [ ] Aplicar autenticacao de observabilidade e escopo de workspace.
- [ ] Validar limites, timeout e planos de consulta.

**Criterio de saida:** respostas reconciliadas com fixtures e com o banco do
canario, dentro do budget de consulta definido.

### Gate 5 — BFF restrito

- [ ] Adicionar somente os GETs aprovados a allowlist.
- [ ] Manter credenciais fora do navegador e do Git.
- [ ] Propagar workspace e filtros de forma fail-closed.
- [ ] Aplicar limites de periodo, tamanho e timeout.
- [ ] Criar testes de rotas permitidas e bloqueadas.

**Criterio de saida:** navegador acessa apenas os contratos read-only previstos.

### Gate 6 — UI de Relatorios

- [ ] Partir do `main` atualizado de `GOHP-LAB/target-extensions-ui`.
- [ ] Reutilizar filtros, autocomplete, canvas e trace existentes.
- [ ] Implementar Visao Geral.
- [ ] Implementar Sessoes.
- [ ] Implementar Acionamentos.
- [ ] Implementar Canais e seus estados nativos.
- [ ] Implementar drill-down acionamento -> sessao -> canvas.
- [ ] Tratar vazio, parcial, indisponivel, loading e erro.
- [ ] Validar responsividade e navegacao por teclado.

**Criterio de saida:** a UI representa corretamente zero, um e varios
acionamentos sem misturar eventos com tentativas.

### Gate 7 — homologacao E2E HighComm

- [ ] Ativar o marco zero somente depois do smoke dos writers.
- [ ] Sessao sem acionamento.
- [ ] Sessao com um acionamento.
- [ ] Varios acionamentos do mesmo canal.
- [ ] Varios canais na mesma sessao.
- [ ] Loop pelo mesmo card.
- [ ] Sucesso, falha, timeout e desfecho de telefonia.
- [ ] Callback duplicado e fora de ordem.
- [ ] Reconciliar previamente o total esperado com API, UI e banco.

**Criterio de saida:** contagens exatas e drill-down coerente para todos os
cenarios controlados.

### Gate 8 — rollout futuro por workspace

- [ ] Manter o relatorio inativo por padrao em workspaces nao homologados.
- [ ] Aplicar migration sem ativar cobertura.
- [ ] Homologar API, workers, callbacks e UI na mesma versao.
- [ ] Gravar `coverage_started_at` apenas depois do smoke completo.
- [ ] Confirmar que a UI desabilita periodos anteriores ao marco.
- [ ] Validar somente sessoes e actions novas, produzidas depois da ativacao.
- [ ] Preservar todos os dados anteriores no banco, sem exibi-los ou migra-los.

**Criterio de saida:** cada workspace inicia uma serie confiavel a partir de
seu proprio marco zero, sem qualquer mistura com o passado.

### Gate 9 — robustez, seguranca e rollout

- [ ] Medir `EXPLAIN`/latencia, p95 e carga concorrente.
- [ ] Validar PII, autorizacao e isolamento de workspace.
- [ ] Definir janela maxima e comportamento para consulta pesada.
- [ ] Executar regressao das APIs e do rastreamento atual.
- [ ] Implantar API de forma gradual, quando aplicavel.
- [ ] Implantar UI pelo script canonico, Node 22 e release atomica.
- [ ] Executar smoke real somente leitura e confirmar rollback.

### Gate 10 — documentacao e retorno ao fluxo

- [ ] Atualizar `PROJECT_BRAIN.md` com o comportamento confirmado.
- [ ] Atualizar arquitetura, banco, runbook e riscos conforme necessario.
- [ ] Registrar evidencias finais no `MAINTENANCE_LOG.md`.
- [ ] Marcar esta frente como homologada ou registrar pendencias objetivas.
- [ ] Retomar `f77b70f0-849b-4d11-9ccc-449b3c4ba981` no checkpoint acima.
- [ ] Reincorporar a homologacao ao objetivo do fluxo completo.

## Disciplina de atualizacao

Para cada gate:

1. marcar itens apenas depois da evidencia;
2. registrar data, ambiente, commit/revisao e IDs observados;
3. registrar resultado objetivo de testes e consultas;
4. separar `CONFIRMED`, `LIKELY` e `UNKNOWN`;
5. anotar decisao e motivo quando o desenho mudar;
6. manter pendencias no gate de origem, sem movê-las silenciosamente;
7. nao abrir o gate seguinte antes de cumprir o criterio de saida atual.

O `PROJECT_BRAIN.md` recebe apenas o resumo executivo e o ponteiro para este
plano. O `MAINTENANCE_LOG.md` recebe fatos de execucao, PRs, deploys e
evidencias de runtime. Este arquivo conserva a sequencia, as decisoes e o
checkpoint de retomada.

## Referencias de mercado usadas no desenho

- Twilio Segment Journeys: metricas gerais e por etapa, periodo e distincao
  entre usuarios totais e unicos.
- Microsoft Customer Insights Journeys: agregacao por jornada, canal e ativo.
- AWS Journey Metrics: execucao geral, atividade individual, branches, espera
  e metricas nativas por canal.
- Braze SMS/RCS Reporting: separacao entre envio, entrega confirmada, falha,
  rejeicao e interacoes de RCS.
- Genesys Journey Analyzer: analise de passos e comportamento entre canais,
  com foco em caminhos e pontos de friccao.
