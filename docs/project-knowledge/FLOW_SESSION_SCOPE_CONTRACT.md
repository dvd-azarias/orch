# Contrato de escopo de sessão e seleção de canal

## Finalidade

Este documento define o contrato normativo entre os modos `person` e
`channel`, os cards de orquestração, o Perfil de Discagem, a Supplier V2 e o
runtime do ORCH.

Ele existe para impedir que um flow estruturalmente válido produza efeitos com
cardinalidade errada, selecione um canal arbitrário ou contorne regras de
discagem. O contrato é uma precondição do canário multilane
`8b81e493-b39c-4829-8b1e-5bafd00aeb7c` e do retorno ao flow completo
`c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.

Classificação: `ALPHA_FIX_REQUIRED`.

Estado: contrato base implementado e exercitado no canário. A fronteira de
calendário entre dois telefones acrescenta uma espera sistêmica interna,
documentada abaixo, sem criar branch novo no canvas.

## Invariantes

1. `channel` continua sendo um modo suportado e não será eliminado.
2. `person` materializa uma sessão lógica por pessoa e permite trocar o canal
   ativo somente por um seletor explícito.
3. `channel` materializa uma sessão por membro/canal; o membro que originou a
   sessão é uma âncora imutável durante sua execução.
4. O endereço representativo usado para iniciar uma sessão `person` não é uma
   seleção implícita de canal.
5. O seletor nunca altera `linked_actuator`.
6. O card de comunicação é a única autoridade do ORCH que define
   `linked_actuator` no membro contextual.
7. A Supplier é a autoridade sobre contagem, elegibilidade, calendário,
   bloqueios, pausas e limites de discagem.
8. O ORCH é a autoridade sobre sessão, grafo, cursor, branches e escolha
   explícita do membro que será entregue ao próximo card.
9. Uma decisão do flow jamais pode ultrapassar um limite rígido da Supplier.
10. O card legado `send_with_dialer` e a Supplier V1 não serão modificados para
    implementar este contrato.
11. Combinações inconsistentes devem ser recusadas no save/publish com `422`;
    os guards de runtime permanecem como segunda barreira fail-closed.
12. Nenhum fallback silencioso de V2 para V1 é permitido.

## Semântica dos modos

### `channel`

- uma pessoa com dez canais pode produzir dez sessões;
- cada sessão permanece presa ao seu `contact_list_member_id` e endereço;
- um seletor pode validar tipo/label do canal atual, mas não pode trocar o
  membro;
- cards de comunicação podem operar diretamente sobre a âncora, desde que o
  tipo seja compatível;
- dois discadores alcançados pela mesma sessão utilizam o mesmo telefone;
- tentar outro telefone ocorre em outra sessão de canal, sob seleção e limites
  da Supplier;
- efeitos que semanticamente acontecem uma vez por pessoa não são aceitos em
  novas definições `channel`.

### `person`

- uma pessoa produz uma sessão lógica para o flow;
- a sessão começa sem `selected_contact_channel` ativo;
- cards de efeito por pessoa podem executar uma única vez;
- todo consumidor de canal exige seleção explícita compatível em todos os
  caminhos que chegam ao card;
- a seleção permanece ativa até ser substituída ou invalidada;
- `not_found`, `exception` e a desativação do membro selecionado removem a
  seleção;
- dois discadores consecutivos reutilizam o mesmo telefone quando não existe
  novo seletor entre eles;
- para trocar o telefone, o grafo deve passar por um seletor `next_eligible`.

## Taxonomia normativa dos cards

| Classe | Cards/capacidades | `channel` | `person` | Regra adicional |
|---|---|---:|---:|---|
| Agnóstico por sessão | `split_random`, condições, cache e transformações puras | Sim | Sim | O resultado é por sessão: por canal em `channel`, por pessoa em `person`. |
| Efeito por pessoa | `identidade_person`, `create_contact`, `source_list_membership` | Não | Sim | Evita repetição, corrida e branches divergentes entre canais da mesma pessoa. |
| Seletor | `select_contact_channel` com canal atual/primeiro elegível | Sim | Sim | Em `channel`, apenas valida a âncora; em `person`, cria seleção explícita. |
| Seletor de próximo | `select_contact_channel` com `next_eligible` | Não | Sim | Exclui o membro atual e obedece o modo de decisão configurado. |
| Consumidor de canal | SMS, RCS, e-mail, WhatsApp, discador legado e `send_with_dialer_handoff` | Sim | Sim | Em `person`, exige seletor dominante e tipo compatível em todos os caminhos. |
| Consulta dinâmica | `check_restriction_lists` | Sim | Sim | Escopo `current_channel` exige seleção em `person`; escopo `person` não exige. |
| Correlação por entidade | `wait_for_event` genérico atual | Não para novos saves | Sim com unicidade | Até existir correlação explícita por sessão, `channel` é ambíguo. Runtime deve falhar se houver mais de uma candidata. |

Cards não listados devem receber uma classe antes de a validação ser habilitada
para eles. A ausência de classificação nunca significa permissão implícita.

## Estado de seleção do canal em `person`

O validador de grafo e o runtime observam os estados abaixo:

```text
unselected
    |
    | selector.selected
    v
selected(type, member, address)
    |
    | novo selector.selected
    v
selected(novo type/member/address)

selected -- selector.not_found/exception --> unselected
selected -- membro desativado -----------> unselected
```

Regras:

- o estado inicial é sempre `unselected`;
- a seleção contém `contact_list_member_id`, lista, mailing, pessoa, tipo e
  endereço;
- um consumidor exige seleção compatível em todo caminho de entrada;
- o validador não aceita a mera existência de um seletor em outro braço;
- loops devem ser analisados até ponto fixo; estados incompatíveis convergindo
  para um consumidor são rejeitados;
- SMS aceita capacidade telefônica `sms|phone|voice`; RCS, e-mail, WhatsApp e
  voz mantêm suas compatibilidades específicas;
- `source_list_membership(inactive)` invalida a seleção quando desativa o
  membro contextual. O runtime deve limpar o estado no mesmo commit lógico.

## Seleção de primeiro e próximo canal

O `select_contact_channel` receberá uma estratégia explícita:

- `first_eligible`: comportamento determinístico inicial;
- `next_eligible`: exclui o membro atualmente selecionado.

O default retrocompatível é `first_eligible`.

`next_eligible`:

- é permitido somente em `session_mode=person`;
- nunca escolhe novamente o mesmo `contact_list_member_id`;
- retorna `not_found` quando não existe outro membro elegível;
- não considera `linked_actuator` como prova de elegibilidade;
- quando governado por discagem, não pode consultar apenas membros ativos: a
  elegibilidade precisa ser confirmada pela Supplier V2;
- não cria pessoa, canal, membro ou sessão;
- não altera `linked_actuator`.

## Relação com o Perfil de Discagem

Quando `next_eligible` for usado para voz após um discador, a UI apresentará:

**Origem da decisão para trocar o telefone**

- `respect_dial_rule`: **Respeitar o Perfil de Discagem** (default e
  recomendado);
- `flow_override`: **Forçar a troca pelo fluxo**.

O termo “interceptar Dial Rule” não faz parte do contrato porque poderia sugerir
que limites de segurança serão ignorados.

### `respect_dial_rule`

- exige uma decisão Supplier V2 não consumida, referente à mesma sessão,
  revisão e card de origem;
- somente `decision=next_phone` autoriza a procura do próximo telefone;
- decisão ausente, stale, já consumida ou referente a outro card falha
  explicitamente;
- o consumo é idempotente e auditável.

### `flow_override`

- só pode atuar depois que a tentativa corrente estiver encerrada;
- pode sobrescrever a decisão de navegação associada ao resultado;
- exige registro de `decision_source=flow_override`, card de origem e motivo;
- não interrompe um retry interno ainda não terminal da Supplier;
- não ultrapassa limites rígidos e não transforma canal inelegível em elegível.

### Limites não sobrescrevíveis

Nenhum dos dois modos pode ignorar:

- máximo compartilhado por pessoa;
- máximo por telefone;
- hard cap da campanha;
- tentativa `claimed|dialing` concorrente para a mesma pessoa/telefone;
- calendário fechado;
- pessoa pausada;
- telefone bloqueado;
- membro inativo, lista vencida ou mailing desvinculado;
- restrição regulatória ou operacional equivalente.

## Contrato Supplier V2 → ORCH

O feedback terminal precisa separar o fato telefônico da decisão operacional.
O payload mínimo é:

```json
{
  "event_id": "uuid",
  "cycle_id": "uuid",
  "attempt_id": "uuid",
  "session_uuid": "uuid",
  "flow_uuid": "uuid",
  "flow_revision_id": "uuid",
  "component_ref_id": "ref opaca",
  "contact_list_member_id": 123,
  "outcome": "machine",
  "decision": "next_phone",
  "decision_source": "dial_profile",
  "release_mapping_version": "pdial_v1",
  "terminal": true,
  "terminal_reason": "outcome_attempt_limit_reached",
  "dial_profile_revision_id": "uuid",
  "occurred_at": "2026-09-16T12:00:00Z"
}
```

`release_mapping_version` é opcional para compatibilidade com entregas
anteriores, mas, quando presente, precisa ser uma versão explicitamente
conhecida pelo ORCH. A versão integra a identidade idempotente do terminal.

O estado atual não satisfaz esse contrato: a Supplier devolve
`retry_same_phone` enquanto a tentativa não é terminal e, na terminalização,
não transporta `limit_action`. Implementar apenas `next_eligible` no ORCH
produziria um falso sucesso que ignoraria o Perfil.

Precedência mínima:

1. `answered` é terminal e não autoriza nova discagem da pessoa;
2. limite rígido por pessoa ou hard cap produz decisão não sobrescrevível;
3. limite por telefone ou por resultado aplica o `limit_action` publicado;
4. `next_phone` autoriza seleção de outro telefone;
5. `finish_person` encerra a progressão de discagem da pessoa;
6. `pause_person` impede nova seleção até `next_eligible_at`;
7. `block_phone` torna o telefone inelegível e devolve a decisão ao grafo; a
   permanência do bloqueio deve ser definida no snapshot publicado, sem default
   oculto.

As semânticas de duração do bloqueio e da pausa devem estar materializadas no
snapshot antes de habilitar esses dois comportamentos em produção.

## Branches do seletor

- `selected`: encontrou e vinculou um membro elegível;
- `not_found`: não existe outro canal elegível;
- `blocked_by_policy`: existe canal ativo, mas a política não autoriza a troca;
- `exception`: falha técnica ou contrato inconsistente.

`deferred` é uma decisão de transporte da Supplier V2, não uma saída visual do
card. Quando todos os candidatos aplicáveis estão fora do calendário e existe
uma próxima abertura calculável, a Supplier devolve `reason=calendar_closed` e
`next_eligible_at`. O ORCH preserva a seleção corrente e a decisão terminal
ainda não consumida, mantém o cursor no próprio seletor, grava `frozen_until` e
retoma a mesma resolução somente depois desse instante. Portanto, calendário
fechado não percorre `blocked_by_policy`, `not_found` ou `exception`, não exige
um card `Wait` e não encerra a sessão.

`blocked_by_policy` é decisão de negócio e não deve gerar retry técnico.

## Validação estática no Target Core

O `POST`, `PUT`, `PATCH`, publish e rollback de revisão devem aplicar:

1. validação estrutural do `session_mode`;
2. compatibilidade do modo com a classe de cada card;
3. análise de fluxo do estado de seleção;
4. compatibilidade entre tipo selecionado e consumidor;
5. requisitos de correlação;
6. combinações de estratégia e origem da decisão;
7. proibição de mistura V1/V2 já existente.

Erros seguem o envelope `422` atual, associados a `session_mode` e ao campo do
card responsável. Códigos previstos:

- `flow_session_mode_component_conflict`;
- `flow_channel_context_missing`;
- `flow_channel_context_type_mismatch`;
- `flow_channel_selection_strategy_invalid`;
- `flow_callback_correlation_ambiguous`.

Mensagens devem identificar o nome/ref do card e a correção esperada. O
validador usa dominância por caminho; “há um seletor no flow” não é suficiente.

## Compatibilidade e rollout

- definições publicadas existentes continuam executáveis durante a auditoria;
- novas gravações/publicações passam a ser protegidas após o relatório de
  impacto;
- nenhum flow legado será convertido automaticamente;
- o novo comportamento de próximo telefone será opt-in no card;
- defaults preservam `first_eligible` e o comportamento atual;
- V1 recebe somente testes de regressão, não mudanças funcionais;
- runtime permanece fail-closed quando receber definição que contradiga o
  contrato.

## Auditoria de impacto anterior ao `422`

Auditoria somente leitura executada em 2026-09-16 sobre a revisão executável
(`current_revision`; draft apenas quando ainda não havia publicação):

- 60 workspaces ativos consultados;
- 667 flows encontrados;
- 142 flows de orquestração classificados;
- 126 sem `session_mode`, portanto `legacy_channel`;
- 14 com `session_mode=channel`;
- 2 com `session_mode=person`;
- nenhum erro de leitura e nenhum flow de workspace externo ao Highcomm
  apareceu na lista de impacto.

Os seis flows publicados que exigirão correção antes de uma próxima gravação,
publicação ou rollback protegido são:

| Flow | Modo efetivo | Conflito |
|---|---|---|
| `2423e4d4-d600-4e47-8ef2-6d05b30e961a` — Criate Contact | `legacy_channel` | `create_contact` é efeito por pessoa |
| `935caf41-ec7a-43d3-a0d5-07c605d89449` — Demo 008 External API | `legacy_channel` | `create_contact` é efeito por pessoa |
| `4e7340ee-ac17-488d-953f-46c51d7b2cd3` — Identidade | `legacy_channel` | `identidade_person` é efeito por pessoa |
| `67c00879-f9e3-4ed3-82c0-a695970acc2b` — Vincular Contact a Lista | `channel` | `source_list_membership` é efeito por pessoa |
| `4e163399-e9a0-4335-895f-316c6a161299` — Discador v2 | `channel` | `wait_for_event` genérico tem correlação ambígua |
| `f7414852-e4fc-4e5f-8bb6-4e8ec2d317c8` — Wai For Event | `legacy_channel` | `wait_for_event` genérico tem correlação ambígua |

Os dois flows `person` publicados são o flow completo
`c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152` e o canário de e-mail
`48b83883-c653-42d0-9c73-e04d0d14c54b`. A análise de caminhos não encontrou
consumidor de canal alcançável sem seletor compatível nessas revisões.

Esta auditoria não autoriza conversão automática nem interrupção dos seis
flows. Eles continuam executando com a revisão já publicada. A proteção futura
atua nas mutações e deve retornar `422` com correção explícita; nenhuma
alteração de dados será feita como efeito colateral do rollout.

## Matriz mínima de homologação

| Caso | Resultado esperado |
|---|---|
| `channel`, uma pessoa, dois telefones | duas sessões, cada uma presa ao seu telefone |
| `channel`, dois discadores na mesma sessão | ambos usam o telefone âncora; nenhuma troca |
| `channel` com `next_eligible` | `422` no save/publish |
| `person`, consumidor sem seletor em um dos caminhos | `422` apontando o consumidor |
| `person`, dois discadores sem novo seletor | ambos usam a seleção atual |
| `person`, `respect_dial_rule` + `next_phone` | próximo telefone elegível selecionado uma vez |
| `person`, `respect_dial_rule` sem autorização | `blocked_by_policy`/falha explícita, sem discagem |
| `person`, `flow_override` após terminal | troca auditada, respeitando limites rígidos |
| override com limite por pessoa esgotado | bloqueado; nenhuma chamada |
| seletor `not_found` após seleção anterior | seleção anterior removida |
| membro selecionado desativado | seleção removida antes do próximo consumidor |
| `wait_for_event` em `channel` | `422` até correlação explícita por sessão |
| callbacks de lanes A/B | cada callback retoma somente seu card |
| V1 e V2 single-card | comportamento anterior preservado |

## Pontos de atenção adicionais

- `create_contact` não troca implicitamente a pessoa da sessão. Criar outra
  pessoa e depois executar o seletor ainda usa o contexto original; não assumir
  adoção implícita.
- `source_list_membership(active)` não cria sessões filhas e não garante que um
  canal recém-adicionado esteja materializado para seleção imediata.
- o callback genérico atual escolhe a sessão ativa mais recente por
  `flow_uuid + entity`; em caso de mais de uma candidata, o comportamento futuro
  deve falhar fechado até existir token/UUID explícito de correlação.
- retornar ao mesmo card de discador depois de trocar telefone exige uma nova
  geração de ciclo ou uma restrição explícita. A chave atual por sessão,
  revisão e card não deve reaproveitar silenciosamente um ciclo terminal.

## Continuidade obrigatória

Este contrato não cria um projeto paralelo. Sua sequência obrigatória é:

1. auditar impacto nas definições existentes;
2. implementar Supplier V2 e validações sem tocar V1;
3. implementar o seletor e guards ORCH;
4. homologar o canário multilane em `channel` e `person`;
5. executar regressões V1 e V2 single-card;
6. retomar imediatamente o flow completo
   `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.

Qualquer descoberta que bloqueie um desses passos vira subgate do mesmo plano.
Melhorias não bloqueantes permanecem no backlog posterior ao flow completo.
