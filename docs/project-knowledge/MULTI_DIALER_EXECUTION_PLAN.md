# Plano mestre de execução — múltiplos discadores por flow

## Função deste documento

Este arquivo é a fonte única da verdade para a implementação de múltiplos
cards de discador em um mesmo flow de orquestração.

Toda retomada do trabalho deve começar pela leitura de:

1. `Estado atual`;
2. `Próxima ação exata`;
3. `Checklist mestre`;
4. `Registro de evidências`.

Durante os Gates 3A–3C, ler também integralmente
`docs/project-knowledge/FLOW_SESSION_SCOPE_CONTRACT.md`, que contém a matriz
normativa dos modos, cards e decisões de discagem. O presente documento continua
sendo a fonte única de progresso e do ponto obrigatório de retorno.

Não considerar uma etapa concluída somente porque houve implementação, commit,
merge ou deploy. Uma etapa termina apenas quando seu gate de saída possui
evidência executável registrada neste documento.

## Estrela-guia

Este desenvolvimento não é um projeto independente. Ele existe para remover a
limitação de múltiplos discadores e, após sua homologação, deve retornar
imediatamente ao objetivo maior: revisar, ajustar e fazer funcionar
integralmente o fluxo completo
`c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.

O desenvolvimento multilane somente estará encerrado quando:

- o flow canário `8b81e493-b39c-4829-8b1e-5bafd00aeb7c` operar com dois cards
  `send_with_dialer_handoff`, cada qual com seu destino;
- houver isolamento comprovado de seleção, fila, capacidade, métricas e
  callback por card;
- a regressão do caminho legado V1 estiver aprovada;
- o plano registrar explicitamente a transição para a retomada do flow
  completo.

Depois disso, o próximo trabalho obrigatório é adaptar o flow completo. Não
iniciar outro componente, melhoria de UI ou refatoração não relacionada antes
de registrar a decisão explícita de mudança de prioridade.

## Estado atual

- **Data do checkpoint:** 2026-09-16.
- **Classificação:** `ALPHA_FIX_REQUIRED`, por remover uma limitação que impede
  a execução de um cenário real de cliente.
- **Status:** Gates 0 a 5 e entregas T1, T2, T3, O1, K1 e D1 concluídos. O O1
  foi integrado pela PR ORCH `#177`, merge `11159c9`. Kerberos e
  `service_dialer` multilane estão implantados sob gates fail-closed; somente o
  canário está autorizado. A revisão publicada do canário possui dois cards e
  duas lanes no mesmo Grupo de Execução. A primeira execução real revelou que
  `session_mode=channel` preserva corretamente o membro âncora, mas não comprova
  A→B com outro telefone. A revisão adversarial também comprovou que a Supplier
  V2 ainda não transporta o `limit_action` terminal e que implementar apenas
  `next_eligible` faria o ORCH ignorar o Perfil de Discagem. O Gate 3A–3C foi
  inserido para harmonizar cards, `person`, `channel` e Dial Rule antes de
  retomar o Gate 6. O mailing permanece desvinculado durante esse ajuste.
- **Flow de desenvolvimento:**
  `8b81e493-b39c-4829-8b1e-5bafd00aeb7c`.
- **Workspace de desenvolvimento:**
  `ba7eb0ec-e565-447c-8c11-8f870cf72a60`.
- **Flow integrador a retomar:**
  `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.
- **Princípio de rollout:** capacidade aditiva, Supplier V2, ativação canário e
  nenhum fallback para V1.

### Evidência da fotografia inicial

- O flow completo está ativo e contém dois cards legados
  `send_with_dialer`:
  - `e599fb73-18d8-5f2e-9fe9-334a104a7988`;
  - `513da7fc-5dc9-5539-9990-0bf7f37169ff`.
- Os dois cards possuem branches próprios. O segundo pode ser alcançado após
  seleção de outro canal, confirmando que a necessidade não é apenas visual.
- O flow canário está em `draft`, com revisão
  `a90d1671-d6a6-45b8-af7c-0322073fe2a1` e zero componentes.
- Na fotografia do Gate 0, o Target Core:
  - rejeita mais de um `send_with_dialer_handoff`;
  - mantém um único Perfil efetivo por `flow_uuid`;
  - extrai somente o primeiro card novo para o envelope de voz;
  - seleciona ciclos V2 somente por flow/lista, sem escopo de card.
- T2 já remove localmente os três primeiros bloqueios sob flag+allowlist+limite,
  mantendo a seleção por lane para T3.
- O Kerberos K1 publica manifesto por lane com schema, instância, versão, TTL e
  checksum. V1 e `single_v2` preservam o contrato escalar.
- O `service_dialer` D1 supervisiona N lanes em M Grupos de Execução, com
  capacidade global e identidade de ciclo/callback isoladas por card.

## Objetivo funcional

Permitir N cards `send_with_dialer_handoff` em um flow. Cada card representa
uma **Unidade de Discagem** independente, com seu próprio:

- Perfil de Discagem e revisão publicada;
- configuração preditiva;
- agenda;
- destino BOT ou humano;
- campanha técnica do Pool;
- fila/equipe e `queue_voice_uuid`;
- estado de Supplier;
- demanda, capacidade e métricas;
- ciclo, tentativa e callback.

Unidade lógica não significa processo ou worker exclusivo. As unidades são
agrupadas em **Grupos de Execução** quando apontam para a mesma campanha final
e possuem configuração operacional compatível. Portanto:

```text
1 flow de origem -> N cards/lanes -> M grupos de execução, onde 1 <= M <= N
```

O caso `N lanes -> 1 grupo` é válido e esperado quando vários cards entregam
na mesma campanha final. O caso `N lanes -> N grupos` atende campanhas finais
distintas. Em ambos, existe um único container e um único processo principal
para o flow de origem.

A identidade lógica de uma unidade é:

```text
flow_uuid + component_ref_id
```

A identidade canônica de uma **geração executável** da unidade é:

```text
flow_uuid + flow_revision_id + component_ref_id
```

A fila não identifica a unidade e `component_ref_id` continua sendo tratado
como valor opaco, não necessariamente UUID. Dois cards poderão usar a mesma
fila sem serem tratados como o mesmo card. Uma nova revisão do flow gera outra
geração executável, mesmo quando o card conserva o mesmo `component_ref_id`.

## Invariantes aprovadas

1. O ORCH continua sendo autoridade sobre o grafo, o cursor e a retomada da
   sessão.
2. O ORCH continua sendo a única autoridade que define
   `linked_actuator=dialer`.
3. O valor do `linked_actuator` não receberá fila, UUID ou conteúdo composto.
4. Card, fila e destino serão persistidos no ciclo durável Supplier V2.
5. A Supplier selecionará ciclos com escopo inequívoco de card.
6. O Kerberos continuará como controlador do estado desejado. Ele publicará
   um manifesto; não executará diretamente loops de discagem.
7. O `service_dialer` terá um supervisor de lanes e Grupos de Execução. Estado
   de ciclo permanece isolado por card; recursos físicos compatíveis podem ser
   compartilhados pelo grupo.
8. Haverá limite de capacidade por unidade e um teto global compartilhado.
9. Falha de uma unidade não poderá encerrar nem contaminar as demais.
10. O card legado `send_with_dialer` e a Supplier V1 permanecerão inalterados.
11. O primeiro rollout não permitirá mistura de cards V1 e V2 no mesmo flow.
12. Contrato ausente, inconsistente ou desconhecido será bloqueado sem
    fallback para V1.
13. Alteração de revisão não poderá fazer um ciclo antigo usar configuração de
    fila ou Perfil de uma revisão nova.
14. Métricas operacionais deverão possuir dimensão de flow e unidade, sem
    sobrescrita entre workers.
15. Drafts continuam editáveis durante o drain, mas uma nova revisão não pode
    ser publicada enquanto a revisão corrente possuir ciclos V2 não terminais.
16. O estado desejado será transportado com versão, checksum e TTL; snapshot
    stale ou divergente bloqueará seleção.
17. Cards diferentes que apontem para a mesma campanha final jamais terão seus
    ciclos, callbacks ou branches fundidos; somente o recurso de execução pode
    ser compartilhado.

## Fronteiras de responsabilidade

### Target Core

- validar e persistir N cards novos;
- manter vínculo efetivo de Perfil por card;
- resolver e publicar o destino operacional de cada card;
- persistir ciclos e tentativas Supplier V2;
- selecionar ciclos por unidade;
- aceitar e decidir feedback de cada tentativa;
- nunca reinterpretar o grafo do ORCH.

### ORCH

- percorrer o flow até o card correto;
- marcar `linked_actuator=dialer` no membro contextual;
- materializar ciclo com sessão, revisão, card, Perfil e destino;
- bloquear e retomar a sessão pelo branch do card exato;
- permitir que a mesma sessão alcance posteriormente outro discador.

### Kerberos

- consumir todas as entradas de voz do envelope Target;
- validar e publicar o manifesto versionado de unidades;
- reconciliar container e saúde desejada do flow;
- preservar o contrato escalar legado;
- ativar multilane apenas no modo V2 autorizado.

### `service_dialer`

- supervisionar N lanes distribuídas em M Grupos de Execução;
- monitorar uma vez os recursos compartilhados de cada grupo e manter demanda,
  seleção e correlação isoladas por lane;
- inicializar e selecionar Supplier V2 no escopo do card;
- disputar capacidade por um árbitro global;
- enviar a chamada com campanha, callback e correlação corretos;
- publicar métricas por unidade e agregadas por flow.

## Contratos congelados no Gate 0

Os nomes e a semântica abaixo formam o contrato de implementação. Mudança
posterior exige registro em `Decisões e desvios`, atualização dos testes de
contrato e nova revisão de blast radius.

### Target Core → Kerberos e leitura detalhada do `service_dialer`

O envelope existente de `/v1/workspaces/orch-flows` permanece compatível. O
objeto `orchestrator` passa a expor, para flows V2 autorizados:

```json
{
  "orchestrator": {
    "flow_uuid": "flow de origem",
    "flow_revision_id": "revisão publicada atual",
    "dialer_runtime_mode": "multi_lane_v2",
    "contact_list_id": "lista do flow",
    "voice": [
      {
        "component_id": "send_with_dialer_handoff",
        "component_ref_id": "referência opaca do card",
        "flow_revision_id": "revisão publicada atual",
        "contact_supplier": {
          "contract": "v2",
          "scope": "lane_generation"
        },
        "dial_profile_id": "perfil selecionado no card",
        "dial_profile_revision_id": "revisão publicada efetiva",
        "profile_snapshot_checksum": "sha256",
        "contact_list_id": "lista do flow",
        "flow_uuid": "flow de entrega, mantido por compatibilidade",
        "pool_campaign_uuid": "campanha técnica deste card",
        "runner_token": "contrato existente",
        "helper_runner_token": "contrato existente",
        "copilot_runner_token": "contrato existente",
        "assigned_agents": [],
        "answer_action": {},
        "dialer_configs": {},
        "schedule": {},
        "lane_config_checksum": "sha256",
        "execution_group_checksum": "sha256"
      }
    ]
  }
}
```

Regras do envelope:

- `orchestrator.flow_uuid` é sempre o flow de origem e nunca a campanha de
  entrega;
- o `flow_uuid` histórico dentro de `voice[]` é preservado como flow de
  entrega para não quebrar o card único atual;
- cada card novo gera exatamente uma entrada `voice[]` com identidade
  completa;
- `dialer_runtime_mode` admite `single_v2` e `multi_lane_v2` no caminho novo;
  o envelope V1 não precisa receber este campo;
- `lane_config_checksum` é SHA-256 do JSON canônico dos campos operacionais da
  lane, sem tokens ou timestamps;
- `execution_group_checksum` é SHA-256 conservador da campanha final,
  descritor de entrega/fila e configuração do discador que pode ser
  compartilhada. Identidade do card, Perfil e agenda da lane não são usados
  para fundir ciclos;
- `assigned_agents`, Perfil, campanha do Pool, destino, fila e agenda são
  resolvidos individualmente por card;
- ausência, duplicidade ou divergência de identidade invalida o flow V2; não
  existe escolha implícita do primeiro item;
- tokens continuam somente no envelope detalhado autenticado. O manifesto
  Redis do Kerberos não os replica.

### Kerberos → `service_dialer`

O snapshot existente continua sob `channels.voice`. Para V2 ele recebe um
manifesto aditivo:

```json
{
  "schema_version": 1,
  "publisher_instance_id": "uuid do processo Kerberos",
  "version": 17,
  "updated_at": "2026-09-15T00:00:00Z",
  "checksum": "sha256",
  "workspace_uuid": "...",
  "flow_uuid": "flow de origem",
  "flow_revision_id": "revisão publicada atual",
  "channels": {
    "voice": {
      "runtime_mode": "multi_lane_v2",
      "eligible": true,
      "contact_supplier": {
        "contract": null,
        "valid": false,
        "reason": "multi_lane_requires_supervisor"
      },
      "lanes": [
        {
          "flow_uuid": "flow de origem",
          "flow_revision_id": "revisão publicada atual",
          "component_ref_id": "referência opaca do card",
          "component_id": "send_with_dialer_handoff",
          "lifecycle": "active",
          "eligible": true,
          "reason": "",
          "contact_supplier": {
            "contract": "v2",
            "scope": "lane_generation"
          },
          "contact_list_id": "...",
          "dial_profile_id": "...",
          "dial_profile_revision_id": "...",
          "profile_snapshot_checksum": "sha256",
          "delivery_flow_uuid": "...",
          "pool_campaign_uuid": "...",
          "target_queue_id": "...",
          "live_channel_id": "...",
          "queue_voice_uuid": "...",
          "lane_config_checksum": "sha256",
          "execution_group_checksum": "sha256"
        }
      ]
    }
  },
  "meta": {"ttl_seconds": 120}
}
```

Regras do manifesto:

- um container continua representando um flow;
- o Kerberos avalia **todas** as entradas de voz, sem `break` na primeira
  elegível;
- o container permanece desejado quando houver ao menos um canal saudável;
- agenda ou indisponibilidade operacional bloqueia somente a lane afetada;
- identidade duplicada, mistura V1/V2, contrato desconhecido, cardinalidade
  acima do limite ou checksum estrutural divergente bloqueia o manifesto de
  voz inteiro;
- para uma única lane, o campo escalar `contact_supplier` permanece válido e
  compatível com o runtime atual;
- para múltiplas lanes, o campo escalar é deliberadamente inválido com
  `multi_lane_requires_supervisor`. Assim, uma imagem antiga bloqueia em vez de
  cair na V1;
- `publisher_instance_id + version` ordena snapshots dentro de uma instância.
  Uma nova instância do Kerberos pode reiniciar `version`; `updated_at`, TTL e
  checksum impedem aceitar snapshot stale ou mutação da mesma versão;
- o `service_dialer` refaz a leitura detalhada no Target, associa cada entrada
  pela identidade canônica e compara `lane_config_checksum`. Divergência
  bloqueia somente a lane e gera diagnóstico explícito;
- lanes só podem compartilhar Grupo de Execução quando o
  `execution_group_checksum` e todos os campos que o originaram coincidirem.
  Colisão ou divergência bloqueia o agrupamento, nunca força a fusão.

### `service_dialer` → Supplier V2

As rotas atuais de card único permanecem inalteradas. Multilane usa somente
rotas aditivas:

```text
POST /v2/contact-supplier/{contact_list_id}/lanes/initialize
GET  /v2/contact-supplier/{contact_list_id}/lanes/select
```

Body de `initialize` e query de `select` possuem obrigatoriamente:

```json
{
  "flow_uuid": "...",
  "flow_revision_id": "...",
  "component_ref_id": "..."
}
```

`select` também recebe `limit`. A query SQL filtra simultaneamente pelos três
campos, preserva `FOR UPDATE SKIP LOCKED` e devolve a identidade da geração em
cada resultado. A rota de feedback V2 por token/tentativa permanece a mesma,
pois já é inequivocamente correlacionada ao ciclo.

### Persistência efetiva de Perfil por card

A tabela atual `dialing_profile_effective_flows`, cuja chave é apenas
`flow_uuid`, permanece intocada para compatibilidade do card único. Uma
migration aditiva criará `dialing_profile_effective_flow_components`, com chave
`(flow_uuid, component_ref_id)` e os campos `flow_revision_id`, `profile_id`,
`profile_revision_id`, `bound_at` e `bound_by`.

- card único V2: dual-write na tabela antiga e na nova;
- multilane V2: a tabela nova é autoritativa e a linha ambígua da tabela antiga
  não é criada;
- não haverá backfill destrutivo; flows existentes continuam no contrato atual
  até novo save/publish;
- criação de ciclos multilane valida Perfil pelo par
  `(flow_uuid, component_ref_id)` e pela revisão publicada do flow;
- ciclos continuam contendo snapshot e checksum do Perfil usados no momento da
  materialização.

### Revisão e drain

A primeira entrega usa **drain anterior ao publish**, opção mais segura para o
Alpha:

1. drafts podem ser salvos normalmente;
2. se a revisão corrente de um flow V2 possuir qualquer ciclo em
   `pending|ready|deferred` ou tentativa em `claimed|dialing`, o publish de uma
   nova revisão responde `422` no padrão do Target Core, com contagens em
   `meta` e erro associado ao flow/card;
3. o operador pausa a origem de novos ciclos — no canário, desvincula o
   mailing — e permite que os ciclos atinjam `terminal|failed|cancelled`;
4. somente então a nova revisão é publicada e uma nova geração entra no
   manifesto;
5. desligar flag/allowlist interrompe novas seleções, mas não apaga ciclos,
   tentativas, callbacks nem auditoria;
6. callbacks de tentativas já aceitas continuam válidos durante rollback.

Não haverá, nesta primeira entrega, worker concorrente de duas revisões do
mesmo card. Drain automático com gerações simultâneas fica fora do escopo até
haver necessidade operacional comprovada.

### Supervisão, agentes e capacidade

- `orch.py` continua iniciando um único serviço de discagem por container;
- esse serviço passa a ser um `DialerLaneSupervisor` opt-in;
- o supervisor constrói N lanes e as distribui em M Grupos de Execução;
- cada grupo compatível possui um worker supervisionado, `CampaignConfig`,
  Pool, monitoramento de agentes/fila, preditivo e estado físico próprios;
- dentro do grupo, cada lane conserva agenda, Perfil, identidade Supplier,
  demanda, limite, ciclo, callback e métricas próprios;
- a escolha inicial é uma thread gerenciada por Grupo de Execução, coerente
  com as chamadas bloqueantes existentes. Não usar threads soltas sem
  supervisor;
- mesma campanha final **não basta sozinha** para fundir workers. Também devem
  coincidir campanha técnica do Pool, descritor BOT/humano e fila, além dos
  parâmetros operacionais do discador. Em dúvida, criar grupos separados;
- falha de worker reinicia apenas o grupo afetado, com backoff exponencial
  limitado; as lanes de outros grupos seguem ativas;
- o Kerberos mantém login/logout virtual por flow usando a união dos agentes
  das lanes elegíveis. Cada grupo monitora uma única vez sua campanha/fila;
- agentes presentes em duas lanes não geram login duplicado nem logout enquanto
  ainda pertencerem à união elegível;
- um árbitro thread-safe reserva capacidade antes do `select` da Supplier;
- limite efetivo por lane é o menor entre demanda preditiva e `max_channel`;
- chamadas ativas de uma campanha compartilhada são contadas uma única vez no
  grupo, nunca uma vez por card;
- limite global considera chamadas em progresso mais reservas ainda não
  refletidas pelo PBX;
- a distribuição inicial é round-robin justa, sem pesos. Capacidade não usada é
  liberada imediatamente após seleção/makecall;
- não há chamada quando o teto global é zero, ausente ou inválido.

### Métricas e PDIAL

- o evento agregado `dialer_metrics` permanece um por flow e conserva
  `campaign.uuid/name` como identidade do flow de origem;
- o supervisor agrega totais e agentes sem dupla contagem e inclui
  `dialer_execution_groups[]` e `dialer_lanes[]`; cada lane informa seu
  `execution_group_checksum`, `component_ref_id`, revisão, campanha do Pool,
  fila, demanda, seleção, chamadas ativas atribuídas, estado e erro;
- eventos de chamada recebem o mesmo objeto de correlação da lane;
- a campanha/flow de entrega é dimensão adicional e nunca substitui a origem
  no PDIAL;
- somente o supervisor publica o heartbeat agregado, impedindo
  `last-writer-wins` entre workers.

## Estratégia de compatibilidade

### V1

- Um card legado continua usando o entrypoint, envelope e Supplier atuais.
- Nenhuma query ou callback V1 será alterado para suportar multilane.
- A suíte V1 deverá passar antes e depois de cada mudança do repositório
  `orchestrator`.

### V2 de card único

- O flow V2 já homologado com um card continuará aceito.
- O contrato atual será preservado durante a transição ou adaptado por uma
  camada compatível explicitamente testada.

### V2 multilane

- Somente cards `send_with_dialer_handoff`.
- Ativação explícita por feature flag e allowlist do flow canário.
- Rotas Supplier V2 escopadas por unidade, sem alterar semântica das rotas V1.
- Imagem/runtime novo; nenhuma promoção automática para containers legados.

## Checklist mestre

### Gate 0 — congelamento do desenho

- [x] Confirmar a necessidade real no flow completo.
- [x] Confirmar que o flow canário está vazio.
- [x] Identificar os bloqueios no Target Core, Kerberos e `service_dialer`.
- [x] Aprovar o princípio de Unidade de Discagem.
- [x] Aprovar preservação absoluta da V1.
- [x] Aprovar este plano mestre como fonte da verdade.
- [x] Congelar o envelope Target → Kerberos.
- [x] Congelar o manifesto Kerberos → `service_dialer`.
- [x] Congelar identidade, Grupos de Execução e política de revisão/drain.
- [x] Definir feature flags, allowlist, limites e defaults fail-closed.
- [x] Registrar matriz de branches/PRs e ordem de merge.

**Gate de saída:** contratos escritos, casos de compatibilidade e rollback
revisados antes de migration ou runtime. **Resultado: aprovado em documentação;
nenhuma migration ou alteração de runtime foi realizada.**

### Gate 1 — Target Core: persistência e validação por card

- [x] Atualizar a base para `origin/main` e confirmar o head real das migrations.
- [x] Criar migration aditiva conforme os documentos de migration.
- [x] Criar a persistência inerte do vínculo efetivo Perfil ↔ flow ↔ card.
- [x] Preservar o vínculo atual de card único durante a transição.
- [x] Permitir N `send_with_dialer_handoff` válidos sob flag, allowlist e limite.
- [x] Continuar proibindo mistura V1/V2 no mesmo flow.
- [x] Permitir Perfis iguais ou distintos entre os cards.
- [x] Validar destino e Perfil individualmente com `422` por campo/card.
- [x] Expor `component_ref_id` e revisão no envelope de voz.
- [x] Expor destino operacional resolvido por card.
- [x] Calcular `lane_config_checksum` e `execution_group_checksum`
  deterministicamente.
- [x] Permitir múltiplos cards para a mesma campanha final sem colapsar a
  identidade dos cards.
- [x] Bloquear publish/rollback com ciclos não terminais e emitir o `422`
  documentado.
- [x] Aplicar o contrato nos caminhos de create, update/patch, publish e
  rollback de revisão.
- [x] Executar testes unitários, PostgreSQL real e workspace novo para T1;
  repetir após a implementação do contrato T2.
- [x] Provar no escopo de T1 que nenhuma tabela ou rota Supplier V1 mudou.

**Gate de saída:** uma revisão com dois cards pode ser salva/publicada e o
envelope devolve duas unidades completas e inequívocas. **Implementação,
testes locais, merge, deploy com flags desligadas e regressão de card único
foram aprovados; o gate permanece aberto até a prova integrada do flow canário,
após T3 fornecer a seleção por lane.**

### Gate 2 — Target Core: seleção Supplier V2 por unidade

- [x] Acrescentar destino/revisão ao snapshot durável do ciclo.
- [x] Criar as rotas aditivas `/lanes/initialize` e `/lanes/select`.
- [x] Escopar initialize/select por `flow_uuid`, `flow_revision_id` e
  `component_ref_id`.
- [x] Impedir que a unidade A selecione ciclo da unidade B.
- [x] Preservar `FOR UPDATE SKIP LOCKED` e idempotência.
- [x] Preservar limites compartilhados por pessoa e telefone.
- [x] Definir comportamento de ciclos de revisão anterior.
- [x] Cobrir concorrência real entre duas unidades.
- [x] Cobrir ausência/inconsistência de unidade como fail-closed.
- [x] Manter callbacks V2 correlacionados a ciclo e tentativa.

**Gate de saída:** seleção concorrente comprova isolamento A/B sem perda,
duplicação ou fallback. **Implementação, commit/PR, merge, migration, deploy com
flags desligadas e regressão single-card concluídos. O Target Core está apto;
ativação continua bloqueada até O1, K1, D1 e canário integrado.**

### Gate 3 — ORCH: materialização e retomada por card

- [x] Revalidar o `main` do ORCH antes do branch.
- [x] Manter `linked_actuator=dialer` sem valor composto.
- [x] Materializar o destino do card no ciclo Supplier V2.
- [x] Garantir idempotência por sessão, revisão e card.
- [x] Garantir que o segundo card crie um ciclo independente.
- [x] Garantir que callback do card A não retome o card B.
- [x] Cobrir `channel` e `person` quando aplicável.
- [x] Cobrir rollback transacional do marcador/ciclo.
- [x] Executar testes unitários, PostgreSQL e stack local completa.

**Gate de saída:** uma sessão consegue bloquear e retomar em dois cards
distintos, mantendo ciclos e branches independentes. **Comprovado localmente em
teste encadeado da engine, teste PostgreSQL real de callback corrente/histórico
e regressão da stack completa. A PR ORCH `#177` foi integrada no merge
`11159c9`; a ativação permanece restrita ao canário.**

### Gate 3A — contrato de escopo dos cards

Fonte normativa:
`docs/project-knowledge/FLOW_SESSION_SCOPE_CONTRACT.md`.

- [x] Revisar adversarialmente os novos cards e o plano multilane.
- [x] Definir `channel` como sessão ancorada e `person` como sessão com seleção
  explícita mutável.
- [x] Classificar cards por cardinalidade, consumo de canal e correlação.
- [x] Preservar o card de comunicação como autoridade de `linked_actuator`.
- [x] Definir que o endereço representativo da sessão `person` não equivale a
  canal selecionado.
- [x] Definir análise de dominância por caminho para consumidores de canal.
- [x] Auditar definições existentes e medir o impacto dos futuros `422`.
- [x] Implementar metadados/registro normativo no Target Core (`#514`).
- [x] Proteger create, update, patch, publish e rollback no padrão `422`
  (`#514`), sem bloquear vínculo de mailing em revisão legada já publicada.
- [x] Manter guards fail-closed equivalentes no ORCH (`#180`).

**Gate de saída:** nenhuma definição nova consegue combinar modo e cards com
cardinalidade ou contexto incompatíveis; flows publicados existentes foram
auditados antes da ativação do bloqueio.

### Gate 3B — decisão Dial Rule → Supplier V2 → ORCH

- [x] Separar conceitualmente resultado telefônico de decisão operacional.
- [x] Definir `respect_dial_rule` e `flow_override` sem permitir bypass de
  limites rígidos.
- [x] Confirmar o gap atual: `limit_action` existe no snapshot, mas não é
  executado/transportado no feedback terminal.
- [x] Congelar a semântica durável de `pause_person` e `block_phone` no snapshot
  (`#513`).
- [x] Transportar decisão, origem, Perfil/revisão, membro e motivo terminal no
  outbox Supplier V2 (`#513`/`#179`).
- [x] Fazer `next_phone` depender de autorização/eligibilidade Supplier V2
  (`#514`/`#180`).
- [x] Garantir consumo idempotente da decisão pelo ORCH (`#180`).
- [x] Provar por regressão automatizada que V1 e V2 single-card não mudaram de
  comportamento (`#513`/`#179`).

**Gate de saída:** o ORCH nunca deduz `next_phone` apenas pelo branch do evento;
a decisão usada é explícita, auditável e corresponde ao Perfil/revisão do
ciclo.

### Gate 3C — seletor harmonizado

- [x] Adicionar `first_eligible|next_eligible`, com default retrocompatível
  `first_eligible`.
- [x] Permitir `next_eligible` somente em `person`.
- [x] Adicionar `respect_dial_rule|flow_override` para próximo canal de voz.
- [x] Excluir o membro atual e limpar seleção em
  `not_found|blocked_by_policy|exception`.
- [x] Adicionar branch `blocked_by_policy` sem retry técnico.
- [x] Invalidar seleção quando o membro contextual for desativado.
- [ ] Cobrir o retorno ao mesmo card com nova geração de ciclo ou rejeição
  explícita; nunca reutilizar ciclo terminal silenciosamente.
- [ ] Testar PostgreSQL real, stack local completa e canários controlados.

**Gate de saída:** o seletor troca de telefone somente em `person`, sob decisão
e elegibilidade explícitas, sem alterar `linked_actuator` nem reinterpretar o
grafo.

### Gate 4 — Kerberos: manifesto multilane

- [x] Revalidar o `main` do repositório `orchestrator`.
- [x] Consumir todas as entradas de voz do flow.
- [x] Preservar o contrato escalar legado.
- [x] Publicar `lanes[]` com versão/checksum.
- [x] Publicar `execution_group_checksum` sem replicar tokens no Redis.
- [x] Avaliar agenda e validade por unidade.
- [x] Não interromper unidades válidas por indisponibilidade operacional de
  outra unidade.
- [x] Bloquear integralmente mistura V1/V2.
- [x] Reconciliar adição, alteração e remoção de unidade.
- [x] Manter um container por flow.
- [x] Testar manifesto duplicado/divergente no publicador e stale/ausente no
  consumidor.

**Gate de saída:** Kerberos publica duas unidades independentes e continua
gerenciando flows legados sem alteração observável.

### Gate 5 — `service_dialer`: supervisor de lanes e Grupos de Execução

- [x] Criar `DialerLaneSupervisor` em caminho opt-in.
- [x] Instanciar N lanes e agrupá-las conservadoramente em M grupos.
- [x] Comprovar N lanes → 1 grupo quando campanha final/configuração coincidem.
- [x] Comprovar N lanes → N grupos quando campanhas finais divergem.
- [x] Evitar compartilhamento acidental de ciclos, Supplier, agenda, Perfil,
  métricas ou callbacks entre lanes.
- [x] Compartilhar `CampaignConfig`, Pool, agentes e preditivo somente dentro de
  um Grupo de Execução compatível.
- [x] Criar worker supervisionado por Grupo de Execução.
- [x] Limitar quantidade máxima de workers por flow.
- [x] Reiniciar somente o grupo que falhar, com backoff.
- [x] Implementar encerramento e drain controlados.
- [x] Implementar árbitro de capacidade global e limite por unidade.
- [x] Impedir oversubscription de PBX/tronco.
- [x] Selecionar Supplier V2 no escopo da unidade.
- [x] Usar campanha/fila correta no `makecall`.
- [x] Preservar callback V2 por tentativa.
- [x] Agregar heartbeat por flow sem perder métricas por unidade.
- [x] Acrescentar `component_ref_id`, fila e unidade ao PDIAL/diagnóstico.
- [x] Manter o entrypoint legado sem mudança funcional.

**Gate de saída:** duas lanes compartilham corretamente um grupo no cenário de
mesma campanha; duas campanhas distintas operam em dois workers, uma pode
falhar sem parar a outra e a capacidade total permanece dentro do teto.
**Resultado: aprovado e implantado com ativação restrita; a prova E2E permanece
no Gate 6.**

### Gate 6 — flow canário

- [x] Criar branch/PRs apenas após autorização explícita.
- [x] Desenhar no flow canário dois cards novos.
- [ ] Rodada 1: apontar A e B para a mesma campanha/fila final e comprovar um
  Grupo de Execução com duas lanes isoladas.
- [ ] Rodada 2: configurar Discador A → fila A e Discador B → fila B, com
  campanhas finais distintas, e comprovar dois Grupos de Execução.
- [ ] Usar roteamento determinístico para produzir ciclos em A e B.
- [ ] Ligar um branch de falha de A à seleção de canal e depois a B.
- [ ] Ligar `answered` de cada card ao braço correto.
- [x] Validar visualmente antes de publicar.
- [x] Publicar somente após validação estática e de contratos.
- [ ] Vincular mailing controlado somente quando solicitado.
- [ ] Comprovar seleção, chamada, callback e retomada em A.
- [ ] Comprovar seleção, chamada, callback e retomada em B.
- [ ] Comprovar A → B na mesma sessão.
- [ ] Comprovar chamadas simultâneas nas duas filas.
- [ ] Comprovar PDIAL e métricas por unidade/flow.
- [ ] Desvincular mailing ao final de cada rodada.

**Gate de saída:** canário E2E aprovado com evidência de DB, API, logs, PBX,
callbacks, métricas e estado terminal.

### Gate 7 — regressão e promoção controlada

- [ ] Executar suíte Target Core afetada.
- [ ] Executar suíte ORCH afetada.
- [ ] Executar suíte `orchestrator` V1/V2.
- [ ] Executar canário explícito de um flow V1 de card único.
- [ ] Executar canário explícito de um flow V2 de card único.
- [ ] Confirmar ausência de rotas V1 modificadas.
- [ ] Confirmar ausência de fallback V2 → V1.
- [ ] Revisar observabilidade e alarmes.
- [ ] Registrar procedimento de rollback exercitado.
- [ ] Remover a classificação `em desenvolvimento` somente com todas as
  evidências.

**Gate de saída:** multilane homologado e caminhos V1/card único comprovados.

### Gate 8 — retorno obrigatório ao fluxo completo

- [ ] Registrar formalmente o encerramento do DEV multilane.
- [ ] Reabrir a revisão do flow
  `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.
- [ ] Reauditar componentes, 120 branches e definições atuais antes de editar.
- [ ] Substituir os dois `send_with_dialer` legados por
  `send_with_dialer_handoff`, sem alterar o card legado do produto.
- [ ] Configurar Perfil, destino e fila de cada card.
- [ ] Preservar branches, posições e intenção do blueprint.
- [ ] Validar visualmente antes de publicar.
- [ ] Publicar com mailing controlado.
- [ ] Homologar os dois discadores dentro do cenário completo.
- [ ] Continuar a execução do blueprint a partir do primeiro componente ainda
  pendente.
- [ ] Atualizar o roadmap geral somente após comprovação E2E do flow completo.

**Gate de saída:** o flow completo funciona integralmente ou possui pendências
externas explicitamente documentadas, sem pendência oculta da engine.

## Critérios de aceitação não negociáveis

- Zero seleção cruzada entre unidades.
- Zero callback retomando card incorreto.
- Zero alteração funcional na Supplier V1.
- Zero fallback silencioso V2 → V1.
- Zero oversubscription acima do teto global configurado.
- Zero dupla contagem de chamada/agente quando duas lanes compartilham a mesma
  campanha final.
- Mesmo Grupo de Execução não pode fundir identidade, branch ou callback das
  lanes que o compõem.
- Uma unidade indisponível não paralisa as unidades saudáveis.
- Perfil e rota usados são os fixados para o ciclo/revisão.
- Métricas identificam flow e unidade.
- Rollback interrompe novas seleções sem apagar auditoria.
- `channel` nunca troca silenciosamente o membro âncora.
- `person` nunca alcança consumidor de canal sem seleção explícita compatível.
- `next_phone` nunca é inferido apenas pelo outcome; deve ser decisão Supplier
  V2 ou override auditado do flow.
- Override do flow nunca ultrapassa limite rígido, calendário, pausa ou bloqueio.
- O trabalho retorna ao flow completo depois da homologação multilane.

## Estratégia de branches e PRs

Branches só serão criados mediante autorização explícita do usuário e sempre a
partir do `main` atualizado do respectivo repositório:

| Ordem | Repositório | Branch sugerido | Entrega | Dependência |
|---|---|---|---|---|
| T1 | `target-core` | `feat/multi-dialer-v2-binding-storage` | migration inerte da vinculação Perfil↔card e testes de workspace novo | Gate 0 |
| T2 | `target-core` | `feat/multi-dialer-v2-flow-contract` | validação, dual-write, drain guard, envelope e checksums | T1 |
| T3 | `target-core` | `feat/multi-dialer-v2-lane-supplier` | rotas Supplier V2 `/lanes/*` e isolamento concorrente | T2 |
| O1 | `orch` | `feat/multi-dialer-v2-cycle-intent` | materialização/retomada inequívoca por card | T2 e T3 |
| K1 | `orchestrator` | `feat/kerberos-multi-dialer-manifest` | manifesto de lanes e grupos, flag desligada | T2 |
| D1 | `orchestrator` | `feat/service-dialer-execution-groups` | supervisor, agrupamento, capacidade e métricas | T3 e K1 |
| H1 | documentação/configuração | branch do gate ativo | canário, regressão, rollout e retorno ao flow completo | O1 e D1 |

T1 deve ser PR exclusiva de migration. T2 não pode ser aberto sobre branch
stale ou antes do merge de T1. K1 e D1 ficam separados para permitir rollback
do data plane sem desfazer o manifesto. Nenhum branch acima foi criado neste
Gate 0.

T2 foi criada diretamente do merge T1 `24d88fe`. Ela permanece isolada e não
inclui Supplier `/lanes/*`, ORCH, Kerberos ou runtime do discador.

Cada PR deverá registrar:

- hipótese e menor mudança segura;
- contratos preservados;
- blast radius;
- testes e evidências;
- ordem de deploy;
- rollback.

### Preflight T1 — fotografia de 2026-09-15

- `target-core/origin/main`: `9a670a1` (PR `#509`);
- branch T1 criada diretamente de `origin/main`, sem incorporar o branch local
  anterior: `feat/multi-dialer-v2-binding-storage` em `9a670a1`;
- head canônico de `migrations_target_ws`: `20260914_0003_supplier_v2`;
- não existe migration posterior no `origin/main` fotografado;
- revisão proposta para T1: `20260915_0001_multidial_bind`, com
  `down_revision=20260914_0003_supplier_v2` e menos de 32 caracteres;
- T1 cria somente `dialing_profile_effective_flow_components`, seus checks,
  FKs e índices; não altera nem popula `dialing_profile_effective_flows`,
  Supplier V1 ou tabelas de ciclos;
- tabela, PK e todos os índices devem usar o tablespace do workspace;
- `downgrade` destrutivo continua recusado.

Testes obrigatórios de T1:

1. metadata, `down_revision` e head único do Alembic;
2. execução ignorada fora de `ws_*` e rejeição de schema malformado;
3. SQL exclusivamente aditivo, sem `DROP`, `TRUNCATE` ou `CASCADE`;
4. PK `(flow_uuid, component_ref_id)`, `component_ref_id` não vazio e FKs de
   Perfil/revisão;
5. tablespace explícito para tabela, PK e índices;
6. atualização das asserções de head das migrations anteriores;
7. upgrade PostgreSQL real no workspace de teste e inspeção de catálogo;
8. provisionamento de workspace novo pelo pipeline oficial, incluindo ACK
   terminal `completed|failed`;
9. `migrate_all_workspaces.py --dry-run` e execução pelo comando oficial
   somente no gate de validação autorizado, nunca por Alembic ad-hoc em
   produção.

## Feature flags e rollout

Flags congeladas, todas fail-closed por padrão:

```text
TARGET_DIALER_MULTILANE_V2_ENABLED=false
TARGET_DIALER_MULTILANE_V2_FLOW_UUIDS=
TARGET_DIALER_MULTILANE_V2_MAX_LANES_PER_FLOW=1

ORCH_DIALER_MULTILANE_V2_ENABLED=false
ORCH_DIALER_MULTILANE_V2_FLOW_UUIDS=
ORCH_DIALER_MULTILANE_V2_MAX_LANES_PER_FLOW=1
ORCH_DIALER_MULTILANE_V2_MAX_EXECUTION_GROUPS_PER_FLOW=1

DIALER_SUPPLIER_V2_DISPATCH_ENABLED=false
DIALER_MULTILANE_V2_GLOBAL_MAX_CHANNELS=0
```

- allowlists são CSV de UUIDs normalizados;
- Target e runtime usam a interseção de `enabled + allowlist`; divergência
  bloqueia o flow e gera log com motivo;
- `MAX_LANES`/`MAX_EXECUTION_GROUPS` inválido, ausente ou excedido bloqueia o
  manifesto;
- teto global `0` impede chamada mesmo se as outras flags forem ligadas;
- no canário, começar com `MAX_LANES=2`, `MAX_EXECUTION_GROUPS=2` e teto global
  `1`; elevar temporariamente o teto para `2` somente na prova controlada de
  simultaneidade;
- a flag existente da Supplier V2 continua pré-requisito e não é reutilizada
  como autorização multilane.

Ordem de promoção:

1. T1: migration aditiva e inerte;
2. T2 e T3: Target Core/Supplier aptos, flags desligadas;
3. O1: ORCH apto a materializar o contrato;
4. K1: Kerberos apto a publicar o manifesto, flag desligada;
5. D1: imagem `service_dialer` apta, flags desligadas;
6. allowlists exclusivas do flow canário em Target e runtime;
7. homologação mesma campanha final e campanhas finais distintas;
8. regressão V1 e V2 de card único;
9. habilitação do flow completo somente após sua adaptação no Gate 8.

## Rollback

1. Desvincular o mailing do canário.
2. Desabilitar a flag ou retirar o flow da allowlist.
3. Impedir novas seleções multilane no Kerberos/serviço.
4. Aguardar ou terminalizar de forma auditada as tentativas já aceitas.
5. Preservar ciclos, tentativas, callbacks e métricas para investigação.
6. Reverter apenas a imagem/runtime multilane se necessário.
7. Não alterar nem reiniciar desnecessariamente o caminho V1.
8. Migrations aditivas permanecem instaladas salvo plano explícito de
   downgrade seguro.

## Protocolo de continuidade

Ao final de cada sessão de trabalho:

1. atualizar `Estado atual`;
2. marcar somente itens comprovados;
3. registrar evidências e identificadores;
4. preencher `Próxima ação exata`;
5. registrar bloqueios sem avançar silenciosamente para outro assunto;
6. avaliar atualização de `PROJECT_BRAIN.md`, riscos e maintenance log.

Ao retomar:

1. ler este documento;
2. conferir repositórios/branches e estado implantado;
3. confirmar que a evidência mais recente ainda representa o runtime;
4. executar somente a `Próxima ação exata` ou registrar formalmente a mudança
   de prioridade.

## Registro de evidências

| Data | Gate | Ambiente | Evidência | Resultado |
|---|---|---|---|---|
| 2026-09-15 | 0 | Target DB, somente leitura | Flow completo com dois cards legados e canário draft vazio | confirmado |
| 2026-09-15 | 0 | código local | cardinalidade única no Kerberos, binding por flow e runtime monolane identificados | confirmado |
| 2026-09-15 | 0 | código local dos três repositórios | ciclo V2 já guarda revisão/card; initialize/select ainda escopam somente flow/lista; Kerberos e serviço achatam voz para uma entrada | confirmado |
| 2026-09-15 | 0 | documentação | contratos Target→Kerberos→serviço, rotas `/lanes/*`, drain, flags e matriz T1–H1 congelados | concluído |
| 2026-09-15 | 1/preflight | `target-core/origin/main` | remoto atualizado em `9a670a1`; migration head `20260914_0003_supplier_v2`; working copy local não está em `main` | confirmado |
| 2026-09-15 | 1/T1 | Target Core `main` | PR `#510` mergeada em `24d88fe`; migration cria somente `dialing_profile_effective_flow_components` e não altera o vínculo legado | concluído |
| 2026-09-15 | 1/T1 | pytest unitário | 52 testes de migration aprovados; grafo Alembic com head único | aprovado |
| 2026-09-15 | 1/T1 | PostgreSQL 16 isolado | constraints e tablespaces aprovados em duas execuções; runner oficial percorreu workspace novo até o novo head | aprovado |
| 2026-09-15 | 1/T1 | pipeline de workspace isolado | schema novo recebeu a tabela e publicou ACK terminal `completed` | aprovado |
| 2026-09-15 | 1/T2 | Target Core local | validação multilane, binding por card com dual-write compatível, drain e envelope/checksums implementados sob três gates fail-closed | aprovado localmente |
| 2026-09-15 | 1/T2 | pytest focado | 110 testes de binding, validação, catálogo operacional e repositório Flow aprovados | aprovado |
| 2026-09-15 | 1/T2 | PostgreSQL 16 descartável | 2 testes provaram binding autoritativo multilane, dual-write de card único e drain com ciclos/tentativas reais | aprovado |
| 2026-09-15 | 1/T2 | higiene/revisão | `py_compile` e `git diff --check` aprovados; suíte completa bloqueada na coleta por quatro problemas de baseline/ambiente alheios ao T2 | aprovado com ressalva documentada |
| 2026-09-15 | 1/T2 | Target Core remoto | PR `#511` mergeada em `0e2a692`; branch partiu do merge T1 `24d88fe` e não inclui T3/ORCH/Kerberos/runtime | concluído |
| 2026-09-15 | 1/T1 | Target Core `.239`, banco compartilhado | runner oficial aplicou `20260915_0001_multidial_bind` uma única vez; query posterior confirmou `60/60` workspaces ativos no head e zero divergências | aprovado |
| 2026-09-15 | 1/T2 | Target Core `.239` e `.249` | ambos promovidos para `0e2a692`; backups de `.env` preservados; arquivos `.env` permaneceram byte a byte iguais; CRUD/FULL ativos e health HTTP `200` | aprovado |
| 2026-09-15 | 1/T2 | regressão operacional `.239` e `.249` | catálogo HTTP `200`: 133 flows, 37 envelopes de voz, 36 V1, 1 V2 de card único, zero multilane e zero contrato desconhecido; flow V2 `4e163399-e9a0-4335-895f-316c6a161299` permaneceu `single_v2` com `component_ref_id` | aprovado |
| 2026-09-15 | 1/T2 | latência pós-deploy | catálogo em `1,725 s` no primeiro smoke do `.239` e `0,007647 s` aquecido no `.249`; flow V2 específico em `0,012782 s` e `0,014688 s`, respectivamente | aprovado |
| 2026-09-15 | 1/T2 | auditoria de dados | nove flows incompletos, todos no workspace DEV `ba7eb0ec-e565-447c-8c11-8f870cf72a60`, emitiram `invalid_handoff_action`; o contrato os rejeitou em fail-closed e não houve erro de infraestrutura, binding ou migration | dívida de configuração não bloqueante |
| 2026-09-15 | 2/T3 | Target Core local | rotas `/lanes/*`, snapshot durável, escopo por geração e concorrência A/B implementados em branch isolada; Supplier V1 e rotas single preservados | aprovado localmente |
| 2026-09-15 | 2/T3 | PostgreSQL 16 descartável | `9 passed` no Supplier V2 completo, `4 passed` em regressão T2/storage/limites e `2 passed` no DDL T1/T3; workspace novo e upgrade do head anterior chegaram a `20260915_0002_lane_snapshot` no tablespace correto | aprovado |
| 2026-09-15 | 2/T3 | qualidade | suíte focada `117 passed`, regressão Supplier V1 `69 passed`, `git diff --check` e validação do snapshot fail-closed; teste real encontrou e corrigiu constraint que aceitava checksum nulo | aprovado |
| 2026-09-15 | 2/T3 | Target Core `.239` e `.249` | commit `7b12424`, migration `60/60`, Supplier health HTTP `200`, flags multilane desligadas e tráfego V1 real preservado | aprovado |
| 2026-09-15 | 3/O1 | ORCH local | `214 passed` na regressão focada; prova encadeada bloqueou/retomou a mesma sessão em A e B sem herdar terminal ou payload bruto de A | aprovado |
| 2026-09-15 | 3/O1 | PostgreSQL real | `7 passed` em callback ativo/histórico, roteamento contextual e tasks Supplier V2; callback histórico retornou `resume_required=false` e não alterou B | aprovado |
| 2026-09-15 | 3/O1 | suíte completa | `759 passed`; 26 falhas permaneceram em testes antigos que chamam assinaturas legadas fora do diff O1, sobretudo `trigger_orch(flow_uuid=...)` | aprovado com ressalva de baseline documentada |
| 2026-09-15 | 3/O1 | stack local completa `f5_local` | API, três workers e dois Beats ficaram `up`; `/health/ready` confirmou DB/schema; smoke real aceitou 5 eventos em cada flow A/B e tasks `advance_session` concluíram sem erro; stack foi encerrada sem órfãos | aprovado |
| 2026-09-16 | 3/O1 | GitHub | PR ORCH `#177` mergeada em `11159c9`; ciclos e retomadas permanecem isolados por card | concluído |
| 2026-09-16 | 4/K1 | `orchestrator`/GitHub/Kerberos | manifesto por lane, contrato escalar compatível, flags/allowlist/limites fail-closed; PR `#31` em `306abe9`; watchdog saudável | aprovado |
| 2026-09-16 | 5/D1 | `orchestrator`, `.136` e `.143` | supervisor, grupos, capacidade e métricas implantados na imagem `v63`; 34/34 containers promovidos e controles multilane inicialmente fechados | aprovado |
| 2026-09-16 | 6/publicação | flow canário v1 | dois cards publicados, dois bindings/lanes, checksums distintos e um Grupo de Execução; visual validado | aprovado |
| 2026-09-16 | 6/segurança | Kerberos/runtime | allowlists e limites restritos ao canário; `single_v2` permaneceu fora do dispatch multilane | aprovado |
| 2026-09-16 | 6/primeiro vínculo | Target/ORCH/Supplier V2 | primeiro vínculo encontrou gate ORCH fechado; falhou antes de criar chamada e foi corrigido sem fallback V1 | fail-closed confirmado |
| 2026-09-16 | 6/gap A→B | código e definição | `channel` preserva o membro âncora; seletor atual em `person` escolhe apenas o primeiro ativo e não exclui o atual | gap confirmado |
| 2026-09-16 | 3A–3C | revisão adversarial | catálogo valida campos, mas não compatibilidade modo/card ou dominância; callback genérico é ambíguo em `channel`; Supplier terminal não transporta `limit_action` | contrato corretivo aprovado |
| 2026-09-16 | 3A/auditoria | Target DB, transação read-only | 60 workspaces, 667 flows e 142 orquestrações; modos `126 legacy_channel + 14 channel + 2 person`; seis incompatibilidades, todas publicadas no workspace DEV Highcomm; nenhum erro de leitura | impacto delimitado |

## Decisões e desvios

| Data | Decisão ou desvio | Motivo | Impacto no plano |
|---|---|---|---|
| 2026-09-15 | Multilane será exclusivo do novo card/Supplier V2 | preservar V1 e impedir ambiguidade de seleção | flow completo será adaptado depois do canário |
| 2026-09-15 | `linked_actuator` permanecerá `dialer` | fila mutável no membro quebraria ciclos concorrentes ou sequenciais | destino fica no ciclo V2 |
| 2026-09-15 | Kerberos publica manifesto e o serviço supervisiona workers | separar control plane de data plane | um container/processo por flow, N lanes isoladas |
| 2026-09-15 | Lane não equivale a worker | cards distintos podem apontar para a mesma campanha final | N lanes são distribuídas em M Grupos de Execução; identidade/callback nunca são fundidos |
| 2026-09-15 | Agrupamento é conservador | mesmo destino com parâmetros incompatíveis não pode compartilhar estado físico | checksum e campos operacionais devem coincidir; na dúvida, grupos separados |
| 2026-09-15 | Primeira entrega usa drain antes do publish | impedir que ciclo antigo herde fila/Perfil da revisão nova | draft permitido; publish retorna 422 enquanto houver ciclo/tentativa não terminal |
| 2026-09-15 | Supplier multilane usa rotas aditivas `/lanes/*` | manter card único V2 e V1 reversíveis | rotas atuais permanecem sem mudança semântica |
| 2026-09-16 | `channel` permanece suportado e ancorado | há casos reais de execução por canal; troca silenciosa mudaria cardinalidade | próximo canal é proibido em `channel` |
| 2026-09-16 | `person` começa sem canal selecionado | endereço representativo de bootstrap não é intenção de comunicação | consumidor exige seletor dominante em todos os caminhos |
| 2026-09-16 | Dial Rule e flow têm autoridades distintas | Supplier conhece limites; ORCH conhece o grafo | `respect_dial_rule` ou `flow_override`, sempre sob limites rígidos |
| 2026-09-16 | Revisão semântica vira Gate 3A–3C | impedir falso sucesso no canário e manter o norte do produto | concluir contrato antes de retomar o Gate 6; depois voltar ao flow completo |

## Próxima ação exata

Não vincular mailing ao canário antes do rollout. Integrar primeiro os PRs-base
ORCH `#179` e Target Core `#513`; depois integrar/implantar o consumidor ORCH
`#180` e somente então o catálogo/produtor Target Core `#514`. Confirmar health,
workers e smoke sem mailing. Em seguida, usar
`8b81e493-b39c-4829-8b1e-5bafd00aeb7c` em duas provas controladas: `channel`
com ambos os discadores presos à âncora e `person` com seletor inicial mais
`next_eligible` entre os discadores. Só depois fechar o retorno ao mesmo card e
retomar o flow completo `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.
