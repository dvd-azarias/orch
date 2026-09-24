# Relatorios de Orquestracao — plano mestre

Plano aprovado em 2026-09-23 para criar uma visao analitica de sessoes,
acionamentos e eventos de canal na UI operacional de Gestao de Extensoes.

Este documento e a fonte unica da verdade desta frente. Toda retomada deve
comecar pela leitura de **Status atual**, **Checkpoint de retorno** e do
primeiro gate ainda aberto. Nao considerar um gate concluido sem registrar a
evidencia correspondente neste arquivo.

## Status atual

- **Estado:** `PAUSED_AWAITING_METRICS_CONTRACT` desde 2026-09-24.
- **Motivo:** a equipe da Metrics API assumiu persistencia, consultas,
  agregacoes e UI dos relatorios. Ao ORCH restara emitir eventos conforme o
  contrato que ainda sera fornecido por essa equipe.
- **Frente ativa no produto:** retomada da homologacao do flow canario
  `f77b70f0-849b-4d11-9ccc-449b3c4ba981`.
- **Ultimo gate concluido:** Gate 2 — contrato funcional e de API, encerrado
  em 2026-09-24 sem alteracao de runtime.
- **Classificacao:** `ALPHA_FIX_OPTIONAL`, com beneficio operacional direto
  para suporte e diagnostico. Mudancas de runtime devem permanecer pequenas,
  aditivas e protegidas.
- **Escopo arquivado nesta branch:** investigacao, contrato e um prototipo
  nao integrado de persistencia local. Nao abrir PR nem aplicar migration,
  comando de ativacao, API ou UI a partir desta branch.
- **Escopo futuro do ORCH:** adaptar a taxonomia confirmada e publicar os
  eventos exigidos pela Metrics API, somente depois da documentacao oficial.
- **Fora do escopo inicial:** alertas, agendamento de relatorios, custos,
  atribuicao comercial, BI externo e grandes refatoracoes do runtime.
- **Marco zero aprovado:** o relatorio considera somente sessoes,
  acionamentos e eventos produzidos depois da ativacao explicita do novo
  mecanismo em cada workspace. Nao existe backfill, importacao, inferencia ou
  exibicao de dados anteriores.
- **Handoff obrigatorio para retomada:**
  `METRICS_REPORTING_HANDOFF_PENDING.md`.

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
- A homologacao do flow deve ser retomada agora exatamente depois dessa
  comprovacao, sem esperar a Metrics API, sem reiniciar o desenho e sem
  repetir gates ja fechados.
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
4. A API rejeita `from < coverage_started_at` com `422` e informa o primeiro
   instante consultavel; nao corta periodo silenciosamente.
5. A UI desabilita datas anteriores e mostra permanentemente `Dados
   disponiveis desde ...`.
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

## Experiencia de UI arquivada como referencia

> A construcao desta UI no projeto de Gestao de Extensoes foi cancelada. A
> Metrics API ja possui UI propria. O desenho abaixo permanece somente como
> referencia de requisitos e experiencia para a futura integracao.

Menu proposto:

```text
Extensoes
  Orquestracao
    Rastreamento de Jornadas
    Relatorios
```

`Rastreamento de Jornadas` continua voltado a suporte operacional e uma sessao
especifica. `Relatorios` fornece agregacao analitica e permite chegar ao
rastreamento existente por drill-down.

### Filtros comuns

- workspace selecionado globalmente;
- flow com autocomplete por nome;
- periodo: hoje, ontem, 7 dias, 30 dias ou intervalo customizado;
- revisao do flow;
- pessoa, localizada no autocomplete existente por nome, identificador ou
  canal e enviada ao relatorio como `person_uuid`;
- estado da sessao;
- canal, com multi-selecao;
- card/componente;
- resultado do acionamento;
- sessoes com acionamento, sem acionamento ou todas.

### Visao geral

- sessoes iniciadas, ativas/aguardando, finalizadas, interrompidas e com erro;
- sessoes sem acionamento e com ao menos um acionamento;
- total de acionamentos e pessoas distintas;
- duracao media e percentil 95;
- serie temporal alternando sessoes e acionamentos;
- distribuicao por canal, card e desfecho;
- funil sintetico de sessao, tentativa, entrega/conexao e engajamento.
- banner persistente com o inicio da cobertura confiavel do workspace.

O funil sintetico nao substitui a semantica nativa de cada canal.

### Visao de sessoes

Uma linha por sessao, com flow/revisao, pessoa, inicio, duracao, estado,
quantidade de acionamentos, canais, ultimo card e desfecho. A expansao mostra
os acionamentos em ordem cronologica; o drill-down abre o canvas e o trace ja
existentes.

### Visao de acionamentos

Uma linha por ocorrencia de card de canal, com sessao, pessoa/address
mascarado, canal, card, ordinal da tentativa, estado final, latencia e
identificador do provedor. O detalhe mostra sua linha do tempo de eventos e
leva de volta a sessao.

### Visao por canal

| Canal | Ciclo e resultados desejados |
| --- | --- |
| WhatsApp | `sent`, `delivered`, `read`, `failed`, resposta/interacao quando disponivel |
| SMS | `sent`, `delivered`, `failed`, `rejected`, `expired` quando informado |
| RCS | `sent`, `delivered`, `read`, `failed`, clique/interacao/resposta |
| Voz | solicitado, discado, `answered`, `machine`, `busy`, `no_answer`, `rejected`, `invalid_number`, `failure` |
| E-mail | `sent`, `delivered`, `open`, `click`, `bounce`, `failed`, somente conforme o ciclo for implementado |

## Fronteira tecnica prevista

```text
Navegador
  -> BFF restrito da UI
      -> APIs read-only de observabilidade do ORCH
          -> schema do workspace
```

- A UI nunca acessa banco diretamente.
- Credenciais de observabilidade permanecem somente no BFF.
- As rotas novas sao adicionadas a allowlist explicita do BFF.
- Agregacao, paginacao, mascaramento e limites de periodo ocorrem no servidor.
- O namespace deve estender a observabilidade existente, sem criar um segundo
  rastreador de sessoes.

Rotas candidatas, sujeitas ao contrato do Gate 2:

- `GET .../observability/reports/summary`;
- `GET .../observability/reports/timeseries`;
- `GET .../observability/reports/sessions`;
- `GET .../observability/reports/actions`;
- `GET .../observability/reports/channels`;
- `GET .../observability/reports/actions/{action_id}`.

## Persistencia candidata

> **Nao integrar.** A migration `0023`, o repository e o service presentes
> nesta branch sao um prototipo validado, mas a ownership de persistencia
> passou para a Metrics API. Servem apenas para recuperar invariantes e casos
> de teste quando o contrato externo chegar.

O levantamento deve primeiro determinar a cobertura real de:

- `orch_sessions` — ciclo da sessao e timestamps resumidos;
- `orch_session_metrics` — passagens repetidas pelos cards;
- `orch_channel_events` — ledger de callbacks de canal.

Essas fontes nao correlacionam de maneira deterministica varias ocorrencias do
mesmo card/canal na mesma sessao. A extensao segura sera um ledger de
acionamentos chamado `orch_channel_actions`, com:

- `action_id` idempotente;
- sessao, flow e revisao;
- `component_ref_id`, tipo do componente e ordinal da visita/tentativa;
- canal, pessoa e destino mascaravel;
- estado corrente/final e desfecho;
- identificadores externos do provedor;
- timestamps do ciclo de vida;
- somente atributos escalares previstos no contrato; payloads e metadados
  livres nao entram no ledger;
- indices por workspace/flow/tempo, sessao, canal e estado.

Uma tabela de controle registra `coverage_started_at`. Somente actions criadas
pelo runtime novo depois desse instante entram nas APIs. As fontes atuais
foram auditadas apenas para desenhar a fronteira; elas nao serao importadas.

## Evidencias do Gate 1 — 2026-09-24

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

### Decisao do Gate 1

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

O Gate 2 deve decidir se a projecao sera escrita sincronicamente nas fronteiras
ja duraveis ou alimentada por tarefas idempotentes. Nao deve consultar ou
agregar `orch_session_metrics` como fonte primaria de acionamentos.

## Gates e checklist

> Gates 3 a 9 estao **suspensos e substituidos** pelo futuro contrato da
> Metrics API. Seus itens nao devem ser executados como originalmente
> descritos. Gate 10 foi antecipado somente para registrar este congelamento e
> devolver o foco ao flow canario.

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

**Estado:** suspenso. Existe prototipo local nao integrado, sem PR, migration
aplicada ou deploy. A decisao final de persistencia pertence a Metrics API.

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

**Estado:** cancelado no ORCH; responsabilidade assumida pela Metrics API.

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

**Estado:** cancelado nesta frente; a UI local de Relatorios nao sera criada.

- [ ] Adicionar somente os GETs aprovados a allowlist.
- [ ] Manter credenciais fora do navegador e do Git.
- [ ] Propagar workspace e filtros de forma fail-closed.
- [ ] Aplicar limites de periodo, tamanho e timeout.
- [ ] Criar testes de rotas permitidas e bloqueadas.

**Criterio de saida:** navegador acessa apenas os contratos read-only previstos.

### Gate 6 — UI de Relatorios

**Estado:** cancelado. A visualizacao sera fornecida pela UI da Metrics API.

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

**Estado:** reformular quando a Metrics API entregar envelope, transporte,
autenticacao, ACK/retry e criterios de aceite dos eventos.

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

**Estado:** reformular segundo o modelo de ativacao/cobertura da Metrics API.

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

**Estado:** reformular depois do contrato da Metrics API.

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
- [x] Marcar a frente como pausada aguardando o contrato da Metrics API.
- [x] Registrar as pendencias objetivas no handoff da Metrics API.
- [x] Liberar a retomada de `f77b70f0-849b-4d11-9ccc-449b3c4ba981` no
  checkpoint acima.
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
