# Homologacao do ciclo de vida de contatos

Plano mestre aprovado em 2026-09-18 para homologar, em canarios isolados, os
cards de criacao, identificacao, vinculo, canais e gerenciamento de contatos
antes de retomar o fluxo completo.

Este trabalho nao autoriza uma refatoracao geral. O ponto de partida e validar
o que ja existe e alterar somente gaps comprovados por teste executavel.

## Norte e sequencia obrigatoria

1. Homologar os cards de contato em canarios simples.
2. Avaliar e corrigir somente gaps comprovados.
3. Portar, no workspace HighComm, o comportamento dos fluxos Velox usados como
   referencia, sem modificar os originais.
4. Retomar e ajustar o fluxo completo
   `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.

O trabalho multilane e seu canario
`8b81e493-b39c-4829-8b1e-5bafd00aeb7c` permanecem preservados, mas estao fora
do foco deste ciclo. Nao misturar as duas homologacoes.

## Workspaces e fluxos de referencia

- Workspace de homologacao HighComm:
  `ba7eb0ec-e565-447c-8c11-8f870cf72a60`.
- Fluxo completo temporariamente pausado:
  `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.
- Workspace Velox, inicialmente somente leitura:
  `253148c7-a85f-42a3-bc8b-5ffd9d885efe`.
- Velox `CREATE_CUSTOMER`:
  `160c13c8-3eb4-40ba-99e7-d3bdddec885b`.
- Velox `ACIONADOR`:
  `652ee631-888e-46f9-843e-d80543051801`.

## Invariantes aprovadas

1. Cards de contato nao criam sessoes implicitamente.
2. Criar ou atualizar canal nao aciona esse canal.
3. Vincular uma pessoa a uma lista nao inicia campanha.
4. Somente um card atuador explicito gera comunicacao.
5. Uma sessao pode criar ou localizar o contato e aciona-lo na mesma execucao
   somente quando o canvas declarar um atuador posterior.
6. Ingestao para uso posterior ou em lote deve terminar e deixar outro fluxo
   produzir as sessoes de acionamento.
7. `person` e o modo preferencial para jornadas que descobrem, administram ou
   escolhem entre varios canais.
8. `channel` continua valido para jornadas ancoradas em um endereco exato; a
   sessao nao troca silenciosamente de canal.
9. Persistir varios canais nunca significa criar varias sessoes.
10. Pessoa, canal, associacao a lista, materializacao operacional e sessao sao
    contratos diferentes.
11. O mesmo telefone pode pertencer a pessoas diferentes. Nao criar trava
    global para corrigir carga incorreta do cliente.
12. Nenhum fan-out pode ser efeito colateral de cadastro, enriquecimento ou
    vinculo.

## Modelo funcional congelado

### Tempo real na mesma sessao

```text
Trigger
  -> localizar/criar pessoa
  -> criar ou selecionar canal
  -> opcionalmente vincular a lista
  -> atuador explicito
  -> continuar/encerrar a mesma sessao
```

### Ingestao e acionamento desacoplados

```text
Fluxo de ingestao
  -> criar/enriquecer pessoa
  -> persistir canais
  -> vincular a lista
  -> encerrar

Fluxo de acionamento
  -> consumir lista
  -> produzir sessoes
  -> selecionar canal
  -> atuar
```

## Baseline operacional do Gate 0

Levantamento somente leitura realizado em 2026-09-18:

- working copy ORCH local em
  `e3425706bfbb6e8cf485c29e47799df9ca656a2f`, branch historico
  `docs/target-extensions-ui-handoff`, com alteracoes e artefatos anteriores ja
  presentes; nada deve ser descartado ou sobrescrito para limpar a arvore;
- ORCH em `10.1.20.237` no merge
  `b791f2d7a4b1afc29708d47620c51bd615d89fea` (`#189`,
  `feat/channel-dispatch-gate2`);
- Target Core em `10.1.20.239` e `10.1.20.249`, ambos no merge
  `e5ccdb6e07e64e411b5979614d9ac4a9afd91ed9` (`#529`,
  `fix/refresh-profiles-on-calendar-publish`);
- APIs e processos Celery principais foram observados ativos. Nenhum servico
  foi reiniciado e nenhuma configuracao foi alterada neste gate;
- `.env`, backups e artefatos locais existentes nos servidores pertencem ao
  ambiente e nao devem ser removidos ou normalizados por este trabalho.

O commit Git implantado e uma evidencia do codigo no diretorio, mas cada gate
que depender de runtime deve tambem comprovar processos reiniciados depois da
alteracao correspondente.

## Gates

### Gate 0 — Controle e preservacao

- [x] Persistir o plano e o checklist.
- [x] Registrar o fluxo completo como objetivo de retomada.
- [x] Separar este ciclo da homologacao multilane.
- [x] Inventariar a working copy sem descartar alteracoes existentes.
- [x] Registrar os commits implantados de ORCH e Target Core.
- [x] Nao modificar runtime, fluxo ou dado funcional.

### Gate 1 — Auditoria dos contratos existentes

- [x] Auditar catalogo, validacao e envelope no Target Core.
- [x] Auditar engine, persistencia e branches no ORCH.
- [x] Auditar o ingresso pelo webhook canonico.
- [x] Auditar `create_contact`.
- [x] Auditar `identidade_person`.
- [x] Auditar `source_list_membership`.
- [x] Auditar `select_contact_channel`.
- [x] Determinar como `person_uuid`, identificador, lista e canal sao
      propagados para cards posteriores.
- [x] Produzir matriz `esperado x atual` sem alterar codigo.

#### Evidencia e conclusoes do Gate 1

Auditoria somente leitura concluida em 2026-09-18 contra os commits realmente
implantados registrados no Gate 0. O comportamento documentado abaixo vem do
codigo executado nesses commits; historicos de homologacao foram usados apenas
como evidencia complementar.

##### Fronteira `person` x `channel`

- O Target Core aceita `session_mode=channel|person`; na ausencia do campo, o
  fallback legado continua sendo `channel`.
- `create_contact`, `identidade_person` e `source_list_membership` sao
  estaticamente exclusivos de `person`. Um flow `channel` com qualquer um deles
  recebe `422 flow_session_mode_component_conflict`.
- `select_contact_channel` e valido nos dois modos. Em `channel`, ele somente
  pode preservar/selecionar o membro que originou a sessao e nao aceita
  `next_eligible`. Em `person`, pode trocar entre membros da mesma pessoa,
  lista e mailing.
- Em `person`, todo caminho que chega a um atuador precisa ter passado pelo
  branch `selected` de um seletor compativel. O Target Core recusa o save com
  `flow_channel_context_missing` ou `flow_channel_context_type_mismatch`.
- Inativar o membro contextual por `source_list_membership` invalida a selecao;
  um atuador posterior exige nova selecao.

##### Ingresso pelo webhook canonico

- `POST /v1/orch/{workspace_uuid}/{flow_uuid}` aceita um objeto JSON e, quando
  nenhuma integracao mais especifica reconhece o payload, usa `GenericApp`.
- `external_id` define `entity`, `entity_address` e `entity_session_id`; sem ele,
  o ORCH gera um UUID aleatorio. O payload inicial fica disponivel em
  `input_payload`, `variables.payload` e tambem no nivel raiz de `variables`.
- O ORCH nao consulta o `session_mode` da definition para esse POST direto. O
  runtime le `session_scope` do proprio payload e assume `channel` se ele faltar.
- O ingresso generico nao cria `contact_list_member` nem transforma dados de
  contato do payload em uma ancora operacional.
- No runtime implantado, toda sessao `person` sem pelo menos um seletor de
  membro/lista/mailing valido termina antes de executar o primeiro card com
  `contact_member_scope_not_found`. Portanto, o caso novo
  `webhook cru -> criar/localizar pessoa` ainda nao consegue iniciar em
  `person`.
- Tentar contornar isso omitindo `session_scope` faria o runtime executar como
  `channel`, divergindo da definition e do contrato estatico. Isso nao e uma
  solucao valida para os canarios.

##### Contratos reais dos cards

| Card | O que atende hoje | Saida para cards seguintes | Limite confirmado |
|---|---|---|---|
| `create_contact` | `update_current`, `create_if_missing` e `upsert`; politicas `fill_missing` e `overwrite_non_null`; dados cadastrais e `extra.*`; concorrencia protegida por unicidade de `identifier` | `variables.customs[output_var]` com `action`, `person_uuid`, `identifier` e `changed_fields` | Nao possui `lookup_only`, nao grava canais/listas/sessoes e nao promove a pessoa criada para `variables.contact` |
| `identidade_person` | Consulta externa, `lookup_only`, create/enrich/upsert, normalizacao de CPF, telefones/e-mails, DND, lista opcional e vinculo protegido com o flow | `variables.customs[output_var]`; o UUID local fica em `local_action.person_uuid` | Nao promove o resultado para `variables.contact`; depende de conseguir chegar ao card, o que o bootstrap `person` sem membro hoje impede |
| `source_list_membership` | Associa idempotentemente pessoa a `source_list`; `active|inactive`; reativa/inativa membros ja materializados somente no flow corrente; para sessoes irmas no `inactive` | `variables.customs[output_var]` com estado, lista, draft, contagens e `sessions_created=0` | `active` nao cria `contact_list_member` ausente, nao vincula a lista ao flow e nao cria sessao; associacao organizacional nao equivale a materializacao operacional |
| `select_contact_channel` | Selecao deterministica por tipo/label; ancora exata em `channel`; troca controlada em `person`; integracao Supplier V2 para proximo telefone | `variables.customs[output_var]`, `workflow_v2.selected_contact_channel` e atualizacao de `variables.contact` no sucesso | Exige `contact_list_member`, `contact_list_id` e `mailing_id` ja existentes; nao parte apenas de `person_uuid` ou dos canais de `persons` |

##### Propagacao de contexto

- `variables.contact` e `variables.customs.contact` sao montados a partir de um
  `contact_list_member` ativo resolvido para a sessao. Eles nao sao atualizados
  por `create_contact` nem por `identidade_person`.
- A saida de `create_contact` pode ser consumida explicitamente, por exemplo,
  como `{{contact_action.person_uuid}}` no card de lista.
- A saida da Identidade tambem e consumivel por template, no caminho configurado
  em `output_var`, mas o UUID fica aninhado em `local_action.person_uuid`.
- Somente `select_contact_channel`, depois de encontrar um membro materializado,
  atualiza o canal/pessoa correntes e, em `person`, reancora o endereco da mesma
  sessao.
- A associacao protegida da Identidade com o flow chama o Target Core de forma
  assincrona, pode materializar membros e usa `skip_orch_sessions=True`. Ela nao
  gera fan-out nem uma segunda sessao.

##### Matriz `esperado x atual`

Legenda: `ATENDE`, `PARCIAL`, `BLOQUEADO` ou `INTENCIONAL`.

| Cenario esperado | Estado atual | Evidencia/impacto |
|---|---|---|
| Webhook recebe uma pessoa nova e o primeiro card a cria | `BLOQUEADO` | `person` sem membro termina antes do card; `channel` e rejeitado pelo Target Core |
| Localizar pessoa local sem criar nem atualizar | `PARCIAL` | O lookup por identificador existe internamente, mas `create_contact` nao expoe uma acao somente localizar |
| Criar pessoa por identificador | `PARCIAL` | A engine e idempotente e concorrente, mas somente funciona quando a sessao ja possui contexto `person` valido |
| Atualizar pessoa corrente | `ATENDE` | `update_current` foi homologado com pessoa ja ancorada; campos vazios nao apagam dados |
| Upsert cadastral por identificador | `PARCIAL` | Persiste corretamente e produz UUID, mas nao adota o contato como contexto corrente |
| Criar/adicionar/atualizar canais vindos do webhook | `BLOQUEADO` | Nao ha operacao generica de canal; `create_contact` deliberadamente nao altera canais |
| Criar canais vindos da Identidade | `PARCIAL` | Identidade normaliza e persiste canais, mas o bootstrap sem membro e a falta de adocao de contexto impedem o uso imediato no novo cenario |
| Vincular pessoa a lista apenas para organizacao | `ATENDE` | `source_list_membership active` cria/reutiliza draft e vinculo sem sessao implicita |
| Vincular e tornar a pessoa imediatamente elegivel no flow corrente | `PARCIAL` | Reativa membros existentes, mas nao cria membro materializado ausente; a trilha especial da Identidade consegue solicitar refresh protegido no Target Core |
| Inativar pessoa na base materializada do flow | `ATENDE` | Escopo limitado a flow/lista/pessoa, preserva dados e encerra somente sessoes irmas ativas |
| Selecionar canal e atuar na mesma sessao | `ATENDE` se ancorado; `BLOQUEADO` no bootstrap novo | O seletor exige membro/lista/mailing existentes e o Target exige selecao antes do atuador em `person` |
| Reutilizar a saida de um card no seguinte | `ATENDE` de forma explicita | `customs[output_var]` funciona; nao existe promocao automatica para `contact.*` |
| Evitar fan-out ao cadastrar, enriquecer ou vincular | `ATENDE` | Os cards nao criam sessoes; a trilha Identidade usa `skip_orch_sessions=True` |
| Preservar semantica de `channel` | `ATENDE` | Cards de pessoa sao barrados e o seletor nao troca para outro membro |
| Mesmo canal em pessoas diferentes | `ATENDE` | Nao existe trava global de endereco; a identidade funcional permanece pessoa/lista/membro |
| Duplicidade exata de identificador | `ATENDE` | `persons.identifier` e a insercao idempotente impedem duas pessoas para o mesmo identificador exato |

##### Gaps comprovados a levar ao Gate 2

1. Definir um bootstrap seguro para sessoes `person` iniciadas sem
   `contact_list_member`, restrito a jornadas de descoberta/criacao.
2. Definir como a pessoa localizada/criada passa a ser o contato corrente da
   mesma sessao sem criar outra sessao.
3. Definir uma operacao generica e idempotente de canais para payloads que nao
   venham da Identidade.
4. Definir se `create_contact` ganha `lookup_only` ou se a localizacao local deve
   ficar em outro contrato generico.
5. Definir a materializacao operacional opcional depois do vinculo a lista,
   sempre sem fan-out.
6. Tornar explicita a origem de `session_scope` no webhook direto para impedir
   divergencia silenciosa entre payload e definition.

Esses itens sao achados, nao autorizacoes de implementacao. Nenhum codigo,
flow, dado funcional ou servico foi alterado durante o Gate 1.

### Gate 2 — Contrato funcional definitivo

- [x] Congelar precedencia entre `person_uuid`, identificador externo e canal.
- [x] Congelar normalizacao de telefone, e-mail e tipos de canal.
- [x] Congelar politicas de preenchimento, sobrescrita e campos vazios.
- [x] Separar associacao organizacional de materializacao operacional.
- [x] Congelar o formato de saida consumido por cards seguintes.
- [x] Definir exatamente o subconjunto valido em `channel`.
- [x] Definir validacoes `422` para combinacoes impossiveis.

#### Contrato aprovado do Gate 2

Contrato aprovado em 2026-09-18. Ele orienta canarios e patches futuros, mas
nao altera retroativamente o runtime implantado.

##### Identidade e correlacao

1. `external_id` pertence a requisicao/sessao e serve para correlacao e
   idempotencia do webhook; nunca identifica uma pessoa por si so.
2. `person_uuid` valido e nao mesclado e a referencia local mais forte.
3. `identifier` e a chave de negocio usada para localizar ou criar a pessoa.
4. Quando `person_uuid` e `identifier` forem informados, ambos precisam apontar
   para a mesma pessoa. Divergencia segue por conflito/exception e nunca faz
   merge implicito.
5. Canal nao identifica pessoa. O mesmo telefone ou e-mail pode pertencer a
   pessoas diferentes; canal somente pode ser criado/alterado depois que a
   pessoa estiver determinada.

##### Modo efetivo da sessao

1. A revision fixada do flow e a fonte da verdade para `person|channel`.
2. O payload pode declarar `session_scope` somente como assercao. Se divergir
   da revision, o ingresso deve falhar fechado e ser diagnosticavel.
3. Flow legado sem `session_mode` preserva fallback `channel`.
4. Uma sessao `person` pode iniciar sem membro materializado em estado
   `unbound`, exclusivamente para atravessar cards de descoberta/criacao e
   preparacao de contato.
5. O estado `unbound` nao autoriza selecao nem atuacao. Antes de qualquer card
   de comunicacao, a mesma sessao precisa adotar pessoa, materializar membro e
   selecionar canal explicitamente.

##### Localizacao e dados cadastrais

1. `create_contact` permanece o componente generico de dados da pessoa e ganha
   a acao aditiva `lookup_only`; nenhum novo card somente para lookup.
2. Saidas deterministicas: `found`, `created`, `updated`, `unchanged`,
   `not_found`, `conflict` e `exception`. Branches novas nao alteram o
   comportamento das definicoes existentes.
3. `fill_missing` preenche somente valor ausente.
4. `overwrite_non_null` substitui somente quando o valor recebido for nao nulo
   e nao vazio.
5. Nulo ou vazio nunca apaga dado. Remocao futura exigira acao explicita fora
   deste contrato.
6. Sucesso local de `create_contact` ou `identidade_person` adota a pessoa na
   mesma sessao em um contexto sem canal. Isso atualiza `contact.person_uuid`,
   identificador e dados cadastrais, mas nao simula membro nem endereco.

##### Canais

1. Sera usado um unico componente generico `manage_contact_channels`; nao
   criar cards separados para telefone, WhatsApp, SMS, RCS ou e-mail.
2. O componente opera somente sobre uma pessoa ja resolvida e aceita uma lista
   de canais com tipo, endereco, label, prioridade/principal e estado.
3. Operacoes iniciais: `upsert` e `deactivate`. Exclusao fisica fica fora do
   contrato Alpha.
4. Telefone usa a normalizacao canonica atual do ORCH: somente digitos e remocao
   do prefixo brasileiro `55` quando o valor vier com 12 ou 13 digitos.
5. E-mail e normalizado com trim e lowercase.
6. Tipos canonicos: `voice`, `whatsapp`, `sms`, `rcs` e `email`; capacidade de
   transporte nao muda silenciosamente o tipo persistido.
7. Chave idempotente de canal: pessoa + tipo + endereco normalizado. Label ou
   prioridade podem ser atualizadas sem duplicar o canal.
8. Nenhuma operacao de canal cria lista, membro operacional, sessao ou
   acionamento.

##### Lista e materializacao

1. `source_list_membership` continua sendo o unico card de gerenciamento da
   pessoa em uma lista e passa a expor proposito explicito:
   `organization_only|current_flow_operational`.
2. `organization_only` cria/atualiza somente pessoa, draft, canais do draft e
   associacao com a `source_list`.
3. `current_flow_operational` tambem garante o vinculo da lista com o flow,
   solicita ao Target Core a materializacao do membro e espera conclusao antes
   de prosseguir.
4. A chamada operacional deve ser autorizada pela revision publicada e sempre
   usar `skip_orch_sessions=True`, inclusive quando o vinculo ja existir.
5. A materializacao nunca cria outra sessao. Ela apenas fornece a ancora que a
   sessao corrente podera selecionar.
6. `inactive` continua restrito ao flow/lista/pessoa atuais, preserva dados,
   invalida a selecao corrente e encerra somente sessoes irmas ativas.

##### Contexto e saidas

1. Todo card persiste saida segura em `variables.customs[output_var]` e um
   marcador diagnostico no runtime.
2. A pessoa adotada passa a existir em `variables.contact` e
   `variables.customs.contact`, inicialmente sem `channel.address`.
3. Materializacao operacional adiciona ao runtime a lista, mailing e membros
   elegiveis, sem escolher silenciosamente um canal.
4. `select_contact_channel` continua sendo o unico componente que escolhe e
   adota `channel.address`.
5. Cards posteriores podem usar tanto `contact.*` quanto a saida explicita do
   card anterior; os dois caminhos devem apontar para o mesmo `person_uuid`.

##### Subconjunto valido em `channel`

- `create_contact`, `identidade_person`, `manage_contact_channels` e
  `source_list_membership` sao exclusivos de `person`.
- `select_contact_channel(first_eligible)` pode existir em `channel`, mas fica
  limitado ao membro que originou a sessao.
- `next_eligible`, troca de pessoa/membro e bootstrap `unbound` sao proibidos em
  `channel`.
- Um flow simples de discador/atuador continua valido em `channel` sem seletor,
  preservando o contrato legado.

##### Ordem obrigatoria para acionar na mesma sessao

```text
identificar/adotar pessoa
  -> gerenciar canais
  -> disponibilizar na lista/flow
  -> selecionar canal
  -> atuador explicito
```

Cards opcionais podem existir entre as etapas, mas nenhum atuador pode ser
alcancado sem membro materializado e canal selecionado.

##### Validacoes estaticas previstas

- `422 flow_session_scope_mismatch` quando o payload afirmar modo diferente da
  revision publicada.
- `422 flow_session_mode_component_conflict` para card exclusivo de pessoa em
  flow `channel`.
- `422 flow_person_context_missing` quando um card exigir pessoa antes da
  adocao.
- `422 flow_operational_membership_missing` quando um seletor/atuador puder ser
  alcancado sem materializacao.
- Manter `flow_channel_context_missing` e
  `flow_channel_context_type_mismatch` para ausencia/tipo de canal.
- Validar `person_uuid + identifier` divergentes em runtime como `conflict`, sem
  merge ou escolha silenciosa.

### Gate 3 — Canarios isolados no HighComm

Criar por APIs oficiais. Acesso direto ao banco para alterar definitions exige
autorizacao especifica.

Os payloads canônicos e sem segredos deste gate ficam em
`docs/project-knowledge/CONTACT_LIFECYCLE_CANARY_PAYLOADS.json`. O arquivo não
é um atalho de persistência: ele é a entrada reproduzível para as APIs oficiais
e contém marcadores explícitos para credencial da Identidade e UUIDs de listas.

Preparação concluída em 2026-09-18:

- `A`, `B`, `C`, `E`, `F1` e `F2` foram validados pelo
  `validate_flow_definition` do código efetivamente implantado no `.239`;
- no mesmo código implantado, as provas negativas retornaram exatamente:
  `422 flow_session_mode_component_conflict` para card de pessoa em
  `channel`, `422 select_contact_channel_configuration_invalid` para
  `next_eligible` em `channel` e `422 flow_channel_context_missing` para
  atuador em `person` sem seleção anterior;
- nenhum payload contém atuador, credencial real ou criação implícita de
  sessão;
- `D` não recebeu um definition artificial: o componente genérico
  `manage_contact_channels` ainda não existe e permanece como gap confirmado;
- `B` só pode ser publicado depois da configuração segura das credenciais;
- `C` e `F1` só podem ser publicados depois da substituição dos UUIDs sentinela
  por uma lista canária real;
- a criação dos drafts depende de uma sessão autenticada na API oficial e não
  será substituída por escrita direta no banco.

Execução pela UI oficial em 2026-09-18:

- o canário `A` foi criado no workspace HighComm com o UUID
  `75a5372a-89c7-45c5-9037-6773a5fa3564`;
- foi publicado como `v1`, em modo `person`, sem card atuador;
- o definition contém somente `create_contact`, configurado como `upsert` por
  `{{payload.contact.identifier}}`, política `overwrite_non_null`, projeção de
  `full_name`, `country`, `state` e `city`, e saída `contact_action`;
- evidência da API oficial observada pela UI: `POST /target/v2/flow` = `201` e
  os `PUT /target/v2/flow/{uuid}/draft` necessários = `200`;
- a primeira tentativa de edição, UUID
  `5f76eaa1-4eba-4311-8de5-16e6adc5975c`, não ficou disponível na listagem de
  rascunhos e não será usada como evidência de canário criado.
- o canário `B` foi criado com UUID
  `f64891f1-5ac9-4b89-8312-f4c437e88ec2`, em modo `person`; em 2026-09-18 o
  usuário confirmou a substituição dos marcadores pelas credenciais reais
  diretamente na UI e o flow foi publicado como `v1`, revisão
  `245e4193-8f2e-46fe-868c-92373f38dfe4`. Nenhum valor secreto foi lido,
  copiado ou registrado neste documento; a criação retornou `201` e as
  gravações do draft retornaram `200`.
- o canário `C` foi criado com UUID
  `dccbd022-9575-43f1-9055-dd1ae990ce19`, em modo `person`, usando a lista de
  teste preexistente `codex_manual_contact_flow_fix2_1779154811`; o card está
  configurado para estado `active` e o flow foi publicado como `v1`, mas ainda
  não foi executado (`POST` = `201`; `PUT draft` = `200`).
- o canário `E` foi criado com UUID
  `7d569fdd-075a-4dd0-8c51-12eb5b735da2`, preservando o modo `channel` e com
  `select_contact_channel` em `voice`/`first_eligible`; foi publicado como
  `v1`, sem atuador e com gravação do draft confirmada em `200`.
- o canário `F2` foi criado com UUID
  `afbd254d-d29b-4323-bce7-07d61ba12c5b`, em modo `person`, com seleção
  `voice`/`first_eligible`, sem atuador, com `PUT draft` confirmado em `200` e
  publicado como `v1`.
- o canário `F1` foi criado com UUID
  `8454c7e0-fd3d-4dd7-ae59-b4eb7e5138c8`, em modo `person`, com
  `create_contact` seguido de `source_list_membership` para os ramos
  `created`, `updated` e `unchanged`; `not_found` e `exception` permanecem sem
  continuação. A UI representou a convergência com dois cards de membership
  semanticamente idênticos, ambos usando a lista de teste
  `codex_manual_contact_flow_fix2_1779154811`, sem atuador e sem execução
  (`POST` = `201`; `PUT draft` = `200`); foi publicado como `v1`.

Publicação segura concluída em 2026-09-18:

- `A`, `C`, `E`, `F1` e `F2` foram publicados individualmente como `v1` pela
  UI oficial;
- a UI confirmou `Versão publicada com sucesso` em cada flow;
- todos permanecem sem card atuador, sem comunicação externa implícita;
- `B` recebeu as credenciais exclusivamente pela UI, sem leitura ou registro
  dos valores pelo agente, e foi publicado como `v1`;
- `D` permanece bloqueado, sem definition artificial.

- [x] A — webhook em `person`: criar, localizar, atualizar e propagar contexto.
- [x] B — Identidade em `person`: lookup, create, enrich, upsert e erros;
      falha determinística sem branch homologada após o deploy.
- [x] C — lista/estado: associar, repetir, inativar, reativar e isolar escopo.
- [x] D — canais: criar, normalizar, atualizar, priorizar e desativar; a
      seleção continua sendo responsabilidade explícita do F2.
- [x] E — `channel`: preservar ancora, comprovar os 422 dos cards exclusivos de
      pessoa e impedir troca/fan-out implicito.
- [x] F — ingestao e acionamento em dois fluxos desacoplados.

Registro dos canarios:

| Canario | Flow UUID | Revision | Session mode | Estado |
|---|---|---|---|---|
| A — webhook/person | `75a5372a-89c7-45c5-9037-6773a5fa3564` | `770262a6-b7f0-41b2-8a09-29d738147432` v2 | person | funcional: contato segue por resultado para término explícito; `unchanged` revalidado em runtime |
| B — Identidade/person | `f64891f1-5ac9-4b89-8312-f4c437e88ec2` | `d81c76f0-a737-4b26-b31d-71cc290d9d13` v2 | person | funcional: `encontrado`, `nao_encontrado` e `exception` possuem términos distintos e foram revalidados |
| C — lista/estado | `dccbd022-9575-43f1-9055-dd1ae990ce19` | `95da658c-0b3d-43fb-9626-f16f51e46f5f` v2 | person | funcional: `changed`/`unchanged` terminam em sucesso e falhas em término de atenção; idempotência revalidada |
| D — canais/upsert | `e65308af-0e8f-4061-9a2d-d609ab7f1b9c` | `b30a196a-377a-4da0-9b46-deb775ddeb57` v1 | person | funcional: pessoa -> canais -> lista -> término; criação, normalização, atualização, idempotência e erro tratado homologados |
| D2 — canais/deactivate | `e4cb0ef6-05fc-4c3b-8eac-9a1c67bb904a` | `4a72c336-ab15-4307-9b53-165e2938af7f` v1 | person | funcional: desativação lógica, promoção do próximo principal e repetição idempotente homologadas |
| E — ancora/channel | `7d569fdd-075a-4dd0-8c51-12eb5b735da2` | `7889ade4-0a75-427d-9e24-2b45872c9992` v2 | channel | funcional: preserva a âncora, distingue `selected` de falha e encerra explicitamente |
| F1 — ingestao | `8454c7e0-fd3d-4dd7-ae59-b4eb7e5138c8` | `45ac1dae-19c5-4660-8326-b4587f3e0e14` v3 | person | funcional: contato -> lista -> término; `updated` e `unchanged` revalidados sem sessão implícita |
| F2 — acionamento | `afbd254d-d29b-4323-bce7-07d61ba12c5b` | `992fa75e-4f96-488f-96ec-5ee9ee63f613` v2 | person | funcional: seleciona canal em `person`, distingue ausência/falha e encerra explicitamente |

### Gate 4 — Baseline sem patches

- [x] Executar todos os canarios publicáveis com a implementacao implantada;
      `D` permanece sem componente e a prova negativa de `B` exige o patch
      antes da reexecução pós-deploy.
- [x] Registrar request, revisao, sessao, branch e variaveis.
- [x] Registrar banco antes/depois e contagens afetadas.
- [x] Repetir para provar idempotencia.
- [x] Confirmar ausencia de fan-out, retry loop e efeito externo oculto; `B`
      revelou retry loop determinístico, já contido e com patch local pronto.
- [x] Classificar cada resultado como: atende; configuracao; defeito; gap;
      intencional; ou V2.

#### Evidencia de runtime do Gate 4 — 2026-09-18

Todos os POSTs foram feitos na API ORCH do host canônico `.237`, pelo endpoint
oficial com workspace. A/C/E/F usaram a lista explicitamente de teste
`60333a24-447d-4d32-9ab5-f0f6fbefcd31` (`mailing_id=1141`); como seus membros
já não estavam elegíveis, B usou o membro `10940`, selecionado somente entre
dados rotulados como canário/teste. Nenhum definition continha atuador.

| Canário/prova | Sessão | Revisão | Resultado observado | Classificação |
|---|---:|---|---|---|
| A, ingresso webhook cru `person` | `8213` / `465e57f3-4ccf-477b-8dc6-453f1f63e45d` | `63c688d6-88be-4574-9d77-dee509807530` v1 | terminou antes do primeiro card, com `last_card_uuid=null` e `contact_member_scope_not_found` | gap de bootstrap confirmado |
| A, pessoa canária nova | `8221` | mesma v1 | `created`, UUID propagado em `contact_action`, sem erro | engine atende quando a sessão já está ancorada |
| A, repetição idêntica | `8225` | mesma v1 | `unchanged`, `changed_fields=[]` | idempotência atende |
| A, alteração não nula | `8226` | mesma v1 | `updated`, somente `full_name` e `city` alterados | atualização atende |
| C, primeira associação | `8215` / `f7313b3b-ca4f-4b04-bb1e-838e8317ea49` | `9b8283fb-40b4-4b08-805c-d129e634a75a` v1 | `changed`, um draft/list link criado, `sessions_created=0`, `materialized_members=0` | organização atende; materialização continua parcial |
| C, repetição | `8218` / `33dda176-e088-4d1d-a88b-f30e15f6329c` | mesma v1 | `unchanged`, nenhum novo vínculo ou sessão | idempotência atende |
| E, duas execuções `channel` | `8216` e `8219` | `ef5e0368-6c57-4236-ae20-c8e7976ec9bf` v1 | preservou e selecionou o mesmo membro `10858`, canal `voice` primário | atende |
| F1, ingresso webhook cru `person` | `8214` / `585c7089-f186-4799-b29a-a943705b10df` | `5331299c-c570-4b4e-8d11-6ef658bbf94e` v1 | mesmo bloqueio pré-card `contact_member_scope_not_found` | gap de bootstrap confirmado |
| F1, criação e lista | `8223` | mesma v1 | `create_contact=created` e membership `changed`; zero sessão implícita | encadeamento explícito atende quando ancorado |
| F1, ramo `updated` v1 | `8227` | mesma v1 | parou após `create_contact` com destino órfão `b727dd76-f0d2-4c7a-97d2-4476c2125afe` | erro de configuração do canário, não defeito da engine |
| F1, ramo `updated` corrigido | `8229` / `212ebe92-ea14-424e-84e4-3fdb2f9e963b` | `46cbdeb4-3055-45c8-bc43-fc9d2b2005ee` v2 | `updated` seguido de membership `unchanged`, fim normal | atende |
| F1, ramo `unchanged` corrigido | `8230` / `c52d3dd9-53a9-4207-8ebf-161b0ceb6a1d` | mesma v2 | ambos os cards retornaram `unchanged`, fim normal | atende e é idempotente |
| F2, duas execuções `person` | `8217` e `8220` | `39e3e38e-3b93-4097-990c-5f462e07508c` v1 | selecionou deterministicamente o mesmo membro/canal primário | atende quando materializado |
| B, `upsert` sobre pessoa existente | `8232` | `245e4193-8f2e-46fe-868c-92373f38dfe4` v1 | `found=true`, HTTP 200 em uma tentativa, `local_action=enriched`, fim normal | atende |
| B, repetição idêntica | `8233` | mesma v1 | mesma pessoa local; contagem final por identificador permaneceu 1 | idempotência de identidade atende |
| B, `upsert` criando pessoa | `8234` | mesma v1 | `found=true`, `local_action=created`, fim normal | atende |
| B, consulta vazia | `8235` | mesma v1 | `found=false`, nenhuma escrita local ou de mailing, fim normal | atende |
| B, documento inválido sem branch `exception` | `8236` | mesma v1 | a task escapou como `WorkflowExecutionError` e a sessão acumulou 735 execuções antes do `unassign` oficial; contagem depois ficou estável | defeito Alpha confirmado e contido |
| B, documento inválido pós-deploy | `8290` / `faedfa91-2ec5-4ec3-b62c-75b1d142444b` | mesma v1 | terminou em `state=3`, cursor limpo e `terminal_failure.code=identidade_person_invalid_document`; cinco métricas permaneceram estáveis e houve exatamente um alarme `workflow_m2_identidade_person_invalid_document` | correção homologada em runtime |

O branch órfão do F1 foi corrigido exclusivamente pela UI oficial. O draft
v2 foi lido antes da publicação e continha somente destinos presentes em
`components`; a UI confirmou a publicação e os dois ramos foram reexecutados.

Evidência consolidada de persistência após as repetições:

- exatamente uma pessoa para `CANARY-CONTACT-A-20260918-H01`;
- exatamente uma pessoa para `CANARY-CONTACT-F1-20260918-H01`;
- exatamente um vínculo draft na lista canária para a pessoa do C;
- exatamente um vínculo draft na lista canária para a pessoa do F1;
- `sessions_created=0` em todas as operações de membership;
- zero alarme nas execuções ancoradas C/E/F1/F2 verificadas;
- nenhum card de comunicação ou efeito externo foi alcançado.

Complemento do canário B:

- o membro de âncora foi escolhido somente entre dados identificados como
  canários; nenhum endereço, documento, token ou payload pessoal foi impresso
  nas evidências;
- as sessões `8232` e `8233` resolveram o mesmo identificador do provedor e a
  mesma pessoa local, com exatamente uma linha final em `persons`;
- a sessão `8236` foi desatribuída pelo endpoint oficial, que alcançou as cinco
  sessões canárias do mesmo endereço. Ela terminou em `state=5`; a contagem de
  executor permaneceu em 735 nas duas leituras posteriores;
- o patch `fix/identidade-terminal-failure` faz qualquer erro
  `identidade_person_*` terminal após as tentativas internas quando não há
  branch, preservando a branch `exception` quando existe. Foi implantado no
  commit `ba207ac` no host canônico `.237` e na API temporária `.136`. A prova
  pós-deploy da sessão `8290` confirmou uma única terminalização, um único
  alarme e contagem estável, sem novo hot loop.

Contagem estritamente anterior à primeira escrita dos dois identificadores não
foi registrada; por isso o item de banco `antes/depois` permanece aberto, embora
as ações `created` e as contagens finais unitárias comprovem a ausência de
duplicidade nas repetições.

#### Conversão dos probes em canários funcionais — 2026-09-18

Após a homologação isolada, os definitions foram completados para que cada
canário represente uma jornada mínima executável e visualmente compreensível.
Nenhum atuador, webhook terminal ou comunicação foi adicionado. Somente cards
`finish_flow` sem exportação foram usados como destinos explícitos.

Auditoria consolidada das revisões publicadas:

| Canário | Topologia publicada | Evidência de runtime |
|---|---|---|
| A v2 | `create_contact -> sucesso/atenção -> finish_flow` | sessão `8295`: `unchanged`, término de sucesso, zero alarme |
| B v2 | `identidade_person -> encontrado/não encontrado/exceção -> finish_flow` | `8300=exception`, `8301=nao_encontrado`, `8302=encontrado`; todas terminaram no card correspondente, sem alarme ou loop |
| C v2 | `source_list_membership -> sucesso/atenção -> finish_flow` | `8292=unchanged`, `8293=changed`, `8294=unchanged`; na criação, vínculo `0 -> 1`, e na repetição permaneceu `1`; `sessions_created=0` |
| E v2 | `select_contact_channel(channel) -> selecionado/atenção -> finish_flow` | sessão `8298`: preservou o membro canário `10730`, tipo `voice`, zero alarme |
| F1 v3 | `create_contact -> source_list_membership -> sucesso/atenção -> finish_flow` | `8296=updated -> unchanged`; `8297=unchanged -> unchanged`; ambas com `sessions_created=0` e zero alarme |
| F2 v2 | `select_contact_channel(person) -> selecionado/atenção -> finish_flow` | sessão `8299`: selecionou o membro canário `10730`, tipo `voice`, zero alarme |

O primeiro disparo do C após a publicação, sessão `8291`, usou incorretamente o
trigger genérico sem `entity`/`entity_address` correlacionáveis com o membro e
foi recusado antes do primeiro card com `contact_member_scope_not_found`. A
reexecução passou a usar a rota oficial de criação explícita de sessão, que
mantém `entity`, endereço e seletores do membro coerentes. Esse registro é
mantido para não confundir falha de preparação do teste com defeito do card.

A auditoria do grafo confirmou que os seis canários então publicados possuem
apenas `finish_flow` como nós sem saída. O F1 também teve removida uma posição
de canvas órfã da revisão antiga. Naquele marco, o canário D permaneceu
deliberadamente ausente porque o contrato `manage_contact_channels` ainda não
existia; a homologação posterior está registrada abaixo.

#### Homologação pós-deploy do canário D — 2026-09-18/19

O catálogo e a engine de `manage_contact_channels`, assim como o bootstrap e a
adoção de pessoa em sessões `person`, já estavam implantados antes desta prova.
Os dois flows foram criados e publicados exclusivamente pela API oficial do
Target Core. Ambos possuem somente términos explícitos, nenhum atuador, nenhum
webhook terminal e nenhum branch órfão.

Topologias publicadas:

```text
D:  create_contact -> manage_contact_channels(upsert)
    -> source_list_membership(active) -> finish_flow

D2: create_contact -> manage_contact_channels(deactivate) -> finish_flow
```

Evidência de runtime no endpoint oficial do ORCH `.237`:

| Prova | Sessão | Branches observados | Resultado |
|---|---:|---|---|
| D, pessoa e canais novos | `8385` / `b8218fff-cc9c-45ee-a650-f17c4311d98c` | `created -> changed -> changed` | término de sucesso |
| D, repetição idêntica | `8386` / `cc5879c8-3b1a-40ce-aab7-78d1b6087d76` | `unchanged -> unchanged -> unchanged` | idempotência integral |
| D, formatos canônicos equivalentes | `8387` / `5c2f40fd-6bbd-4179-be8c-b95d65529c30` | `unchanged -> unchanged -> unchanged` | telefone e e-mail não duplicados |
| D, atualização cadastral e novos endereços | `8388` / `8f95f68a-23a3-4603-9afe-8f7fa55f02b5` | `updated -> changed -> unchanged` | pessoa atualizada, canais adicionados, vínculo preservado |
| D, e-mail inválido | `8389` / `f58aa20a-43f8-43bb-866d-7bb554dd05ac` | `created -> exception` | término de atenção, sem retry |
| D2, desativar canal principal | `8390` / `0a853be9-1991-43f0-b11e-fbbbc0e6754d` | `updated -> changed` | canal inativado e próximo ativo promovido |
| D2, repetir desativação | `8391` / `717b3020-2595-49ba-906d-da8941e9e122` | `unchanged -> unchanged` | desativação idempotente |

Persistência confirmada pela API oficial de `persons`:

- exatamente uma pessoa para `CANARY-CONTACT-D-20260919-H01`;
- telefone e e-mail foram normalizados antes da chave idempotente;
- somente um canal ativo permaneceu como principal;
- após desativar o principal mais recente, ele ficou `inactive`, inválido e
  inalcançável, enquanto o próximo telefone ativo foi promovido;
- a associação organizacional ficou na lista canária
  `60333a24-447d-4d32-9ab5-f0f6fbefcd31` (`mailing_id=1141`);
- as consultas de alarmes dos dois flows retornaram zero registros;
- nenhum card criou sessão filha, selecionou canal ou definiu
  `linked_actuator`.

A seleção operacional permanece separada e já é exercitada pelo canário F2.
Isso preserva a divisão profissional de responsabilidade: o D administra os
canais canônicos da pessoa; o F2 seleciona um membro materializado quando a
jornada realmente precisar consumir um canal.

### Gate 5 — Decisao sobre gaps

- [x] Avaliar escrita generica de canais.
- [x] Avaliar materializacao operacional da pessoa recem-vinculada.
- [x] Avaliar atualizacao do contexto corrente depois de criar/localizar.
- [x] Avaliar protecoes estaticas `person` x `channel`.
- [x] Para cada gap, decidir entre configurar, adaptar, criar card generico,
      documentar ou adiar para V2.

Nenhuma hipotese deste gate autoriza implementacao antes da evidencia do Gate
4 e da aprovacao da decisao individual.

#### Decisões propostas após o baseline

Ordem recomendada, ainda sujeita a aprovação explícita antes de código:

1. **Integridade de definitions (`ALPHA_FIX_REQUIRED`)**
   - Target Core deve rejeitar com `422` qualquer branch cujo `from` ou `to`
     não exista em `components[].ref_id`, usando chave de erro compatível com
     destaque pela UI;
   - ORCH deve manter uma defesa para revisions antigas: `component_not_found`
     precisa produzir falha terminal e alarme diagnosticável, sem ser
     apresentado como conclusão normal;
   - o F1 v1 demonstrou o risco real; o v2 corrigido demonstrou que engine e
     cards funcionam quando o definition é íntegro.

2. **Bootstrap seguro de pessoa sem membro (`ALPHA_FIX_OPTIONAL`)**
   - permitir sessão `person` ainda não materializada somente enquanto percorre
     cards declaradamente seguros de preparação;
   - começar com `create_contact` e `identidade_person`; acrescentar o futuro
     `manage_contact_channels` e o modo organizacional/operacional do card de
     lista quando seus contratos estiverem implementados;
   - seletor ou atuador continuam proibidos até existir membro operacional;
   - nenhuma queda silenciosa para `channel` e nenhum acionamento implícito.

3. **Adoção explícita da pessoa atual (`ALPHA_FIX_OPTIONAL`)**
   - sucesso de `create_contact`/`identidade_person` deve preencher um contexto
     de pessoa não materializada, com origem e UUID rastreáveis;
   - não fabricar `contact_list_member`, lista, canal selecionado ou sessão;
   - conflito entre `person_uuid` e `identifier` deve falhar, sem merge ou
     escolha implícita.

4. **Uso organizacional versus operacional da lista (`ALPHA_FIX_OPTIONAL`)**
   - manter o comportamento atual como `organization_only`;
   - adicionar opção explícita `current_flow`, que vincula/materializa pela API
     oficial do Target Core com `skip_orch_sessions=true` e retoma a mesma
     sessão somente depois de confirmar o membro;
   - continuar retornando `sessions_created=0`; fan-out permanece proibido.

5. **Gerenciamento genérico de canais (`ALPHA_FIX_OPTIONAL`)**
   - criar um único card `manage_contact_channels`, em vez de cards específicos
     por canal;
   - suportar array, normalização, upsert idempotente, prioridade/primário e
     conflito claro;
   - não selecionar nem acionar canal; essa responsabilidade permanece no
     `select_contact_channel` e nos atuadores.

6. **Localização local sem escrita (`ALPHA_FIX_OPTIONAL`)**
   - acrescentar `lookup_only` ao `create_contact`, preservando os branches e a
     saída determinística;
   - `not_found` não cria pessoa nem altera dados.

7. **Canário Identidade (`configuração`)**
   - manter `B` em draft até receber credencial atual por canal seguro;
   - depois reexecutar `lookup_only`, create, enrich, upsert, ausência e erro;
   - nunca persistir token em documento, log, artefato Playwright ou Git.

O Gate 6 deve ser quebrado nesses lotes. O lote 1 vem primeiro porque protege a
plataforma inteira; os lotes 2–4 formam o caminho mínimo
`webhook -> pessoa -> canais -> lista operacional`; os demais completam o
contrato sem criar comportamento implícito.

### Gate 6 — Implementacao cirurgica

Lote 1 (`ALPHA_FIX_REQUIRED`) integrado e implantado em 2026-09-18:

- Target Core `fix/flow-orphan-branch-validation`: create/update/publish
  rejeitam `from`/`to` inexistentes com
  `flow_branch_reference_invalid`; `8 passed` nos testes focados. A regressão
  ampliada executou 534 testes, com `524 passed` e as mesmas 10 falhas
  históricas reproduzidas no `origin/main` sem o patch;
- ORCH `fix/orphan-component-runtime-guard`: revisões antigas terminam com
  `terminal_failure=component_not_found`, cursor seguinte limpo e alarme;
  `160 passed` na regressão focada/ampliada de engine, dispatcher e task;
- o Target Core implantado rejeitou os definitions inválidos durante a
  homologação e aceitou as revisões funcionais sem destinos órfãos;
- o ORCH implantado no commit `ba207ac` também contém a terminalização segura
  de falhas da Identidade sem branch. As sessões `8290` e `8300` comprovaram,
  respectivamente, a defesa terminal e o desvio explícito por `exception`.

Para cada gap aprovado, separadamente:

- [x] Classificar `ALPHA_FIX_REQUIRED`, `ALPHA_FIX_OPTIONAL` ou `V2_ONLY`.
- [x] Partir do `main` atualizado e usar branch propria por repositorio.
- [x] Implementar catalogo/validacao `422` no Target Core, se aplicavel.
- [x] Validar contrato e integrar o Target Core.
- [x] Implementar a menor engine ORCH segura.
- [x] Testar unitario, transacional e em PostgreSQL real fora da sandbox.
- [x] Reiniciar e validar a stack local completa.
- [x] Reexecutar o canario afetado e a regressao dos demais.
- [x] Fazer rollout gradual com rollback registrado.

### Gate 7 — Homologacao final

- [x] Pessoa nova e existente.
- [x] Dados completos e incompletos.
- [x] Campos vazios nao apagam dados sem instrucao explicita.
- [x] Conflito e concorrencia do mesmo identificador.
- [x] Mesma pessoa em listas e flows diferentes.
- [x] Mesmo canal em pessoas diferentes.
- [x] Varios canais da mesma pessoa.
- [x] Modos `person` e `channel`.
- [x] Retry, idempotencia e rollback transacional.
- [x] Saida consumida pelo proximo card.
- [x] Zero sessao ou acionamento implicito.
- [x] Contratos e evidencias documentados.

#### Fechamento adversarial do Gate 7 — 2026-09-19

O fechamento foi executado contra o runtime canônico do ORCH no `.237`, com
auditoria de persistência somente leitura. Nenhum fluxo Velox foi alterado e
nenhum card atuador foi introduzido nos canários de contato.

| Prova | Sessões | Evidência objetiva |
|---|---|---|
| Pessoa nova seguida de atualização vazia | `8440` e `8441` | `created -> unchanged`; uma única pessoa e preservação de nome, país, estado e cidade |
| Concorrência do mesmo identificador | `8434` e `8435` | `created + unchanged`; uma única pessoa final (`3ee771f2-5aa5-47f0-a83d-7e8244f8f64b`) |
| Mesmo telefone em pessoas diferentes | `8436` e `8437` | duas pessoas distintas mantiveram o mesmo canal `voice` ativo; uma associação de lista para cada pessoa |
| Mesma pessoa em listas/flows distintos | `8439` e sua preparação correlata | uma pessoa (`c86bf8cb-9d6c-401b-84ce-a87938ebf511`) com um vínculo na lista `271` e outro na lista `1141` |
| Vários canais, normalização e prioridade | `8385` a `8391` | criação, formatos equivalentes, acréscimo, troca de principal, desativação e repetição idempotente |
| Modos de sessão | canários `E` e `F2` | `channel` preservou a âncora exata; `person` selecionou deterministicamente o membro elegível |
| Saída encadeada | canários `D` e `F1` | `person_uuid` de criação consumido por canais/lista; branches sucessivos e término explícito |
| Ausência de efeito implícito | todas as provas do gate | `sessions_created=0`, zero atuador e zero alarme nas provas finais |

O teste de atualização com todos os valores resolvidos como vazios revelou que
o banco já preservava os dados, mas a engine desviava incorretamente para
`exception`. A correção mínima foi integrada pela PR ORCH `#197`, commit
`021b2d5` e merge `e946afe4a99fa5e6c0d65aca5cacac5f5fde764c`. O contrato
final ficou assim:

- pessoa existente + mapping vazio em `update_current` ou `upsert` retorna
  `unchanged` e não executa `UPDATE`;
- pessoa nova sem qualquer dado resolvido continua falhando fechado;
- `133` testes focados passaram antes do rollout;
- `.136` e `.237` receberam o merge, com hashes de `.env` preservados;
- a prova pós-deploy `8440/8441` confirmou o comportamento no runtime real.

O canal compartilhado entre pessoas evidenciou uma compatibilidade legada que
deve permanecer documentada: `persons.channels` aceita corretamente o mesmo
endereço para pessoas diferentes, mas a projeção escalar legada
`primary_channel_type/value/label` ainda possui unicidade global. Por isso a
segunda pessoa registrou `primary_projection_applied=false`; o canal canônico,
o vínculo com a lista e a seleção dos cards novos continuaram válidos. Isso não
autoriza criar uma trava global de telefone.

Rollback transacional neste gate significa atomicidade de cada operação de
card, e não desfazer cards anteriores de uma sessão inteira. Por exemplo, um
canal inválido não deixou escrita parcial de canal; uma pessoa criada por um
card anterior permanece criada quando um card posterior segue por `exception`,
como determina o modelo atual de workflow.

### Gate 8 — Paridade dos fluxos Velox

- [x] Auditar os dois flows originais somente em leitura.
- [x] Congelar as revisoes analisadas.
- [x] Mapear entradas, branches, APIs, variaveis, listas e efeitos.
- [x] Identificar artificios anteriores aos novos cards.
- [x] Decidir por um ou dois substitutos sem forcar unificacao.
- [x] Criar o substituto no HighComm.
- [x] Reproduzir payloads controlados e comprovar a paridade de contatos sem
      discagem.
- [ ] Homologar releases e callback com discagem canaria explicitamente
      autorizada.
- [x] Nao modificar os flows Velox originais.

A auditoria e a decisão de topologia estão detalhadas em
`docs/project-knowledge/VELOX_CONTACT_FLOW_PARITY_PLAN.md`. A evidência mostrou
que o comportamento atual usa dois flows, mas não possui lote real: cada
webhook é transformado em uma lista unitária e imediatamente vinculado ao
acionador. Para este caso de tempo real, o substituto será um único flow com
atuador explícito; cargas destinadas a uso posterior continuam preservando o
modelo profissional de ingestão e acionamento desacoplados.

#### Preflight do substituto — 2026-09-20

O flow HighComm `12fd033e-5793-4b00-95d1-dfe8867de67f`, nome
`Canário Velox — Alerta Digital v2`, foi publicado na revisão v2
`a9bb2891-80db-4731-a3b9-bbb0e0cf3c14`. A revisão usa `session_mode=person`,
uma lista canária estável e um gate que somente alcança o Dialer quando
`payload.enable_dialer == true`. Todos os payloads desta prova omitiram esse
campo; portanto, nenhuma discagem foi autorizada ou produzida.

O patch ORCH da PR `#198`, commit `5c71de3` e merge `9f02c54`, faz o trigger
direto herdar `session_mode` da revisão quando `session_scope` não é informado
e permite que o adaptador `code_editor` atravesse o estado `person/unbound`
depois que `create_contact` adota a pessoa. O valor explícito do payload
continua prevalecendo e o objeto recebido não é mutado. O merge foi implantado
em `.136` e `.237`, com os `.env` preservados; API e workers afetados foram
reiniciados e o readiness respondeu `200`.

| Caso | Sessão | Resultado observado |
|---|---:|---|
| Campos legados e três telefones | `8461` | `created -> success -> changed -> changed -> selected -> false`; três membros, zero filho e zero atuador |
| Array com cinco telefones | `8462` | uma pessoa, cinco canais/membros, principal selecionado, zero alarme, filho ou atuador |
| Repetição idempotente | `8463` | pessoa, canais e vínculo seguiram por `unchanged`; cinco membros preservados |
| `id_alerta` ausente | `8464` | `create_contact.exception -> atenção`; zero pessoa, canal, vínculo ou filho |
| Telefones ausentes | `8465` | pessoa criada, adaptador desviou para atenção e nenhum membro foi materializado |
| Mesmo telefone em outra pessoa | `8466` | segunda pessoa e membro válidos, sem trava global; projeção primária legada ficou `best effort` |
| Atualização e telefone adicional | `8467` | pessoa `updated`, sexto canal criado, seis membros materializados e principal preservado |
| Duplicata conflitante no mesmo payload | `8468` | `manage_contact_channels.conflict -> atenção`; pessoa preservada, zero canal/membro parcial |

Todas as sessões terminaram em `state=3`, sem falha terminal e sem alarme. O
card de lista materializou o escopo operacional com `sessions_created=0` e o
seletor consumiu a pessoa/lista/canal na mesma sessão. Os flows e dados Velox
permaneceram intocados.

Durante a auditoria, um utilitário temporário usou
`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` sobre conexão via
PgBouncer e contaminou um backend reutilizado; uma tentativa recebeu `500`
antes de persistir sessão. O probe foi corrigido para uma transação local
`readonly=True` sempre finalizada por rollback, e 20 conexões foram verificadas
como graváveis antes da repetição bem-sucedida `8462`. Probes de manutenção não
devem alterar características de sessão em pools compartilhados.

### Gate 9 — Retomada obrigatoria do fluxo completo

- [ ] Retomar `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.
- [ ] Revisar sua definicao contra os contratos homologados.
- [ ] Substituir somente artificios comprovadamente obsoletos.
- [ ] Ajustar tempos de canario para poucos minutos.
- [ ] Executar ponta a ponta.
- [ ] Retomar o cronograma maior do ponto registrado no Gate 0.

## Evidencia minima por caso

Cada linha da matriz de homologacao deve registrar:

- payload sanitizado e resultado HTTP;
- flow, revisao e sessao;
- modo `person` ou `channel`;
- cards e branches percorridos;
- variaveis produzidas e consumidas;
- linhas/tabelas afetadas antes e depois;
- repeticao idempotente;
- logs e alarmes relevantes;
- confirmacao de ausencia de sessao, fan-out ou acionamento inesperado.

## Condicoes de parada imediata

Interromper o gate corrente se ocorrer:

- criacao inesperada de sessao;
- acionamento sem card atuador;
- duplicacao de pessoa, canal ou vinculo;
- alteracao em outra lista, pessoa ou flow;
- troca silenciosa de canal;
- retry/hot loop;
- escrita fora dos dados canarios;
- exposicao de PII ou segredo em log/evidencia;
- divergencia entre revisao publicada e executada;
- divergencia entre codigo testado e processo implantado.

## Regra de progressao

Um gate somente e concluido com evidencia registrada. Descobertas intermediarias
nao mudam o foco silenciosamente: devem ser classificadas, anotadas e submetidas
a decisao antes de abrir trabalho novo.
