# Roadmap de Capacidades do Orquestrador Alpha

Plano aprovado em 2026-09-06 para evoluir o ORCH com mudancas pequenas, isoladas e reversiveis enquanto a V2 e desenvolvida separadamente.

## Regras do plano

- Nao versionar cards do catalogo no Alpha.
- Fixar a revisao executavel por sessao para impedir troca de grafo durante uma execucao ja iniciada.
- Implementar um card por vez, sem refatoracao transversal oportunista.
- Para cada card, concluir primeiro o envelope e a validacao no Target Core e depois implementar a engine no ORCH.
- Usar uma branch por card e por repositorio:
  - Target Core: `feat/<card>-catalog`;
  - ORCH: `feat/<card>-engine`.
- Marcar um item como concluido somente depois de teste automatizado, validacao local e canario/E2E aplicavel.
- Preservar contratos existentes e usar feature flag ou flow canario quando o rollout exigir contencao adicional.
- Reavaliar os componentes avancados somente depois da lista comum.

## Estado geral

| Ordem | Capacidade | Classificacao Alpha | Estado |
|---:|---|---|---|
| 0 | Fixacao da revisao por sessao | `ALPHA_FIX_OPTIONAL` | Concluido |
| 1 | `create_contact` | `ALPHA_FIX_OPTIONAL` | Concluido |
| 2 | `source_list_membership` | `ALPHA_FIX_OPTIONAL` | Concluído |
| 3 | `wait_for_event` | `ALPHA_FIX_OPTIONAL` | Concluído |
| 4 | `split_random` | `ALPHA_FIX_OPTIONAL` | Concluído |
| 5 | `select_contact_channel` | `ALPHA_FIX_OPTIONAL` | Em implementação |
| 6 | `send_with_sms` | A classificar no desenho do envelope | Planejado |
| 7 | `send_with_email` | A classificar no desenho do envelope | Planejado |
| 8 | `fail_flow` | A classificar no desenho do envelope | Planejado |

## Checklist padrao por card

Aplicar este checklist separadamente a cada item de 1 a 8:

- [ ] Semantica, efeitos e branches definidos.
- [ ] Envelope atual do card definido, sem versoes paralelas.
- [ ] Branch Target Core criada do `origin/main` atualizado.
- [ ] Catalogo e validacao `422` implementados no Target Core.
- [ ] Testes do contrato Target Core aprovados.
- [ ] PR Target Core integrado e ambiente alvo atualizado.
- [ ] Branch ORCH criada do `origin/main` atualizado.
- [ ] Engine ORCH implementada com o menor blast radius possivel.
- [ ] Timeout, retry e idempotencia locais definidos quando houver efeito externo.
- [ ] Logs, alarmes e runtime permitem diagnosticar sucesso e falha sem expor segredos.
- [ ] Testes automatizados ORCH aprovados.
- [ ] Stack local completa reiniciada e validada conforme `AGENTS.md`.
- [ ] Canary/E2E confirmou persistencia e efeitos externos aplicaveis.
- [ ] PR ORCH integrado e rollout validado.
- [ ] Documentacao e evidencias atualizadas.
- [ ] Item marcado como concluido no estado geral.

## Item 0 — Fixacao da revisao por sessao

Objetivo: uma sessao iniciada na revisao publicada N deve continuar na revisao N mesmo depois da publicacao da revisao N+1. Sessoes novas usam N+1.

Branch ORCH: `fix/pin-session-flow-revision`. PR: `#142`.

- [x] Risco existente confirmado no bootstrap e no executor M2.
- [x] Confirmado que o bootstrap ja persiste `revision_id`, `revision_version` e `revision_mode` em `runtime_variables.workflow_v2`.
- [x] Confirmado que nao e necessaria migration.
- [x] Implementar leitura de `flow_v2_revision` pelo `revision_id` da sessao, validando o `flow_id`.
- [x] Fazer o M2 executar a revisao fixada e registrar o mesmo identificador nas metricas.
- [x] Fazer eventos/callbacks que consultam o grafo respeitarem a revisao da sessao.
- [x] Preservar compatibilidade de sessoes antigas sem `revision_id`, fixando a revisao selecionada na primeira retomada.
- [x] Falhar de forma terminal e diagnosticavel se uma sessao declarar revisao invalida ou inexistente; nunca trocar silenciosamente para a revisao corrente.
- [x] Testar: sessao na revisao N, publicacao N+1 e retomada ainda em N.
- [x] Testar: sessao nova depois da publicacao usa N+1.
- [x] Testar: revisao fixada invalida nao executa N+1.
- [x] Testar: sessao legada sem pin recebe uma revisao e permanece nela.
- [x] Executar regressao direcionada de workflow, eventos de canal, callbacks, metricas e dispatcher.
- [x] Reiniciar a stack local completa e executar smoke encadeado antes de qualquer homologacao.
- [x] Executar canario controlado com uma sessao pausada entre duas publicacoes.
- [x] Atualizar R7 em `KNOWN_RISKS.md`, `DATA_FLOW.md` e o registro da manutencao.
- [x] Revisao final e estrategia de rollback aprovadas: sem migration; em caso de regressao, reverter o commit funcional `1e3b878` e reiniciar API/workers de workflow de forma gradual.

### Limite conhecido do item 0

A garantia forte se aplica a revisoes publicadas, que permanecem identificaveis no historico. O fallback legado para draft sera preservado para nao quebrar contratos do Alpha; como o Target Core edita o draft existente, uma sessao fixada em draft ainda pode observar alteracoes dentro do mesmo `revision_id`. Bloquear execucao de draft ou transformar cada edicao em revisao imutavel amplia o contrato entre sistemas e fica fora deste patch.

## Item 1 — `create_contact`

Objetivo: permitir que um fluxo crie ou atualize dados cadastrais de uma pessoa de forma explicita, limitada e diagnosticavel, sem assumir responsabilidade por canais, listas, membros ou sessoes filhas.

Contrato concluido:

- acoes `update_current`, `create_if_missing` e `upsert`;
- politicas `fill_missing` e `overwrite_non_null`;
- mapping restrito aos campos cadastrais permitidos de `persons` e a caminhos `extra.<campo>`;
- branches `created`, `updated`, `unchanged`, `not_found` e `exception`;
- saida minima com `action`, `person_uuid`, `identifier` e `changed_fields`;
- escrita transacional somente em `persons`, com tratamento concorrente da criacao por identificador.

- [x] Semantica, efeitos e branches definidos.
- [x] Envelope atual definido no catalogo, sem versoes paralelas.
- [x] Branch Target Core criada do `origin/main` atualizado.
- [x] Catalogo e validacao `422` implementados no Target Core.
- [x] Testes do contrato Target Core aprovados.
- [x] PR Target Core integrado e ambiente alvo atualizado.
- [x] Branch ORCH criada do `origin/main` atualizado.
- [x] Engine ORCH implementada sem reativar os helpers embrionarios de lista/membro/sessao filha.
- [x] Concorrencia e idempotencia local definidas; o card nao possui efeito externo que exija timeout ou retry proprio.
- [x] Logs, branches e runtime diagnosticam sucesso e falha sem ampliar dados sensiveis.
- [x] Testes automatizados ORCH aprovados.
- [x] Stack local completa reiniciada e validada conforme `AGENTS.md`.
- [x] Canarios locais e de producao confirmaram persistencia, branches e restauracao dos dados controlados.
- [x] PR da engine ORCH `#144` e correção de serializacao `#145` integrados; rollout validado nos hosts `10.1.20.136` e `10.1.20.237`.
- [x] Documentacao e evidencias atualizadas.
- [x] Item marcado como concluido no estado geral.

Rollback: interromper novos usos do card e reverter a engine `#144` para retirada completa. A correcao generica de serializacao `#145` pode permanecer; se houver motivo independente para remove-la, ela deve ser revertida separadamente. Nao ha migration. Dados ja gravados em `persons` nao devem ser apagados automaticamente.

Proximo item: `source_list_membership`.

## Item 2 — `source_list_membership`

Objetivo: garantir de forma idempotente que uma pessoa existente esteja em uma `source_list`, sem associar a lista a flows, materializar membros ou iniciar sessões.

Contrato aprovado:

- pessoa por `person_uuid`, incluindo `{{contact.person_uuid}}` e saída de `create_contact`;
- lista pelo UUID público `mailing_id`;
- operação somente aditiva;
- branches `linked`, `already_linked`, `not_found` e `exception`;
- saída com ação, pessoa, mailing, IDs internos do vínculo, quantidade de canais e motivo de ausência;
- persistência transacional em drafts/lista e referências `last_*` da pessoa;
- nenhum endpoint, migration, fila ou efeito externo novo.

- [x] Semântica, efeitos e branches definidos.
- [x] Envelope atual definido no catálogo, sem versões paralelas.
- [x] Branch Target Core criada do `origin/main` atualizado.
- [x] Catálogo e validação `422` implementados no Target Core.
- [x] Testes do contrato Target Core aprovados.
- [x] PR Target Core `#465` integrada e ambiente alvo atualizado.
- [x] Branch ORCH criada do `origin/main` atualizado.
- [x] Engine ORCH implementada com operação aditiva e idempotente.
- [x] Não há efeito externo; concorrência é serializada pelo lock da pessoa/lista e a operação usa savepoint.
- [x] Logs, branches e runtime diagnosticam sucesso e falha sem registrar identificador pessoal.
- [x] Testes automatizados focados e teste transacional em PostgreSQL real aprovados.
- [x] Stack local completa reiniciada e validada conforme `AGENTS.md`.
- [x] Canário/E2E confirmou persistência no fluxo real, idempotência e ausência de fan-out.
- [x] PR ORCH `#147` integrada; rollout do merge `c0b1c35` validado nos hosts `10.1.20.136` e `10.1.20.237`.
- [x] Documentação e evidências atualizadas.
- [x] Item marcado como concluído no estado geral.

Rollback: interromper novos usos do card e reverter a engine. Não há migration. Drafts e vínculos já criados representam dados funcionais e não devem ser removidos automaticamente no rollback de código.

Próximo item: `wait_for_event`.

## Item 3 — `wait_for_event`

Objetivo: pausar uma sessão até o callback genérico esperado ou até um timeout obrigatório, sem criar endpoint, fila, ledger ou chave de correlação paralelos.

Contrato aprovado:

- origem inicial exclusiva `callback`, correlacionada por `flow_uuid + entity` na rota canônica;
- `event_result` literal e comparado sem distinção de maiúsculas/minúsculas;
- timeout entre 1 segundo e 30 dias;
- branches `received`, `timeout` e `exception`;
- saída em `variables.customs[output_var]`, sem copiar a entidade para o resultado;
- callbacks anteriores ao armamento e callbacks com outro resultado não liberam a espera.

- [x] Semântica, efeitos e branches definidos.
- [x] Envelope atual definido no catálogo, sem versões paralelas.
- [x] Branch Target Core criada do `origin/main` atualizado.
- [x] Catálogo e validação `422` implementados no Target Core.
- [x] Testes do contrato Target Core aprovados.
- [x] PR Target Core `#467` integrada e ambiente alvo atualizado.
- [x] Branch ORCH criada do `origin/main` atualizado.
- [x] Engine ORCH implementada reutilizando `callbacks_pending`, `frozen_until`, dispatcher e lock da sessão.
- [x] Timeout e idempotência locais definidos; não há retry de efeito externo próprio do card.
- [x] Logs, métricas, resultado e erro permitem diagnosticar armamento, recebimento, timeout e exceção sem registrar `entity` ou `data` nos logs.
- [x] Testes automatizados focados e teste transacional em PostgreSQL real aprovados.
- [x] Stack local completa reiniciada; smokes encadeados anteriores concluíram em `state=3`, sem alarmes.
- [x] Canary/E2E de um flow publicado confirmou `received` e `timeout` pela rota canônica, sem hot loop ou alarme.
- [x] PR ORCH `#149` integrada; rollout do merge `ce47641` validado nos hosts `10.1.20.136` e `10.1.20.237`.
- [x] Documentação e evidências atualizadas.
- [x] Item marcado como concluído no estado geral.

Concorrência: o callback adquire o mesmo advisory lock do executor antes de alterar o runtime. Quando corresponde à espera ativa, deixa a sessão em `state=0` e remove `frozen_until`, garantindo recuperação pelo dispatcher mesmo se a task publicada antes do commit observar `session_execution_locked`.

Limite Alpha: a correlação genérica existente escolhe uma sessão ativa por `flow_uuid + entity`. Flows que possam manter mais de uma sessão ativa com a mesma entidade devem usar uma entidade única por execução ou não usar este card até existir uma correlação explícita; consulte R32.

Rollback: interromper novos usos e aguardar ou resolver as esperas ativas antes de reverter a engine. Uma sessão ainda posicionada no card seria encerrada como componente não suportado pelo código anterior. Não há migration nem efeito externo a desfazer.

Próximo item: `split_random`.

## Item 4 — `split_random`

Objetivo: distribuir cada sessão por uma única variante A ou B conforme percentuais configurados, mantendo a escolha estável em retries e redeliveries sem introduzir estado compartilhado ou efeito externo próprio.

Contrato aprovado:

- percentuais inteiros entre 0 e 100, com soma exata 100 e extremos aceitos;
- branches `variant_a`, `variant_b` e `exception`;
- bucket determinístico por `flow + session + revision + card`, usando `sha256_mod_100_v1`;
- saída textual em `variables.customs[output_var]` e diagnóstico em `split_random_last_result`;
- percentual como probabilidade por sessão, não como cota exata de lote.

- [x] Semântica, efeitos e branches definidos.
- [x] Envelope atual definido no catálogo, sem versões paralelas.
- [x] Branch Target Core criada do `origin/main` atualizado.
- [x] Catálogo e validação `422` implementados no Target Core.
- [x] Testes do contrato Target Core aprovados.
- [x] PR Target Core `#468` integrada e ambiente alvo atualizado.
- [x] Branch ORCH criada do `origin/main` atualizado.
- [x] Engine ORCH implementada com seleção determinística e validação explícita do grafo.
- [x] Não há timeout, retry ou efeito externo próprio; retries do executor recalculam o mesmo bucket.
- [x] Logs, resultado e erro permitem diagnosticar variante, bucket, revisão e configuração sem registrar o material do seed.
- [x] Testes automatizados ORCH aprovados; falhas da suíte ampla foram comparadas com a baseline.
- [x] Stack local completa reiniciada e smoke encadeado validado conforme `AGENTS.md`.
- [x] Canary/E2E confirmou as duas variantes, terminalidade, ausência de alarmes e os POSTs posteriores no destino.
- [x] PR ORCH `#152` integrada e rollout validado.
- [x] Documentação e evidências atualizadas.
- [x] Item marcado como concluído no estado geral.

Rollback: interromper novos usos do card antes de reverter a engine e reiniciar API/workers. Não há migration nem dado funcional externo criado pelo card; sessões ainda posicionadas nele seriam tratadas como componente não suportado pelo código anterior.

Próximo item: `select_contact_channel`.

## Item 5 — `select_contact_channel`

Objetivo: tornar explícita a escolha do canal que uma sessão utilizará, preservando a cardinalidade atual em `channel` e permitindo que uma sessão `person` alcance com segurança um card de comunicação posterior.

Contrato aprovado:

- `channel_type` obrigatório em `voice`, `whatsapp`, `sms` ou `email`; `phone` persistido é normalizado para `voice`;
- `channel_label` opcional, literal e com correspondência exata;
- `output_var` opcional, com default `selected_channel`;
- branches `selected`, `not_found` e `exception`;
- em `channel`, a seleção fica presa ao membro e endereço que originaram a sessão;
- em `person`, exige `person_uuid` e considera somente membros ativos da mesma pessoa, lista e mailing, priorizando `is_primary` e depois o menor `contact_list_member_id`;
- uma seleção `person` altera somente `orch_sessions.entity_address`, com guards de escopo, atividade e colisão; o card não altera `linked_actuator`;
- cards de comunicação posteriores recebem o membro selecionado e continuam sendo a autoridade para definir `linked_actuator`;
- sucesso persiste o objeto selecionado em `variables.customs[output_var]`; ausência persiste `null` e segue `not_found`.
- `not_found` ou `exception` invalidam qualquer seleção anterior, impedindo reutilização silenciosa de um canal antigo.

- [x] Semântica, efeitos e branches definidos.
- [x] Envelope atual definido no catálogo, sem versões paralelas.
- [x] Branch Target Core criada do `origin/main` atualizado.
- [x] Catálogo e validação `422` implementados no Target Core.
- [x] Testes do contrato Target Core aprovados.
- [x] PR Target Core integrada e ambiente alvo atualizado.
- [x] Branch ORCH criada do `origin/main` atualizado.
- [x] Engine ORCH implementada com seleção determinística, rebind guardado e roteamento contextual do membro escolhido.
- [x] Não há timeout, retry ou efeito externo próprio; retries repetem a mesma escolha enquanto os candidatos não mudarem.
- [x] Logs, resultado, erro e alarmes permitem diagnosticar o desfecho sem registrar o endereço do contato nos logs.
- [x] Testes automatizados ORCH aprovados; as falhas da suíte ampla foram comparadas com a baseline.
- [x] Stack local completa reiniciada e smoke encadeado validado conforme `AGENTS.md`.
- [ ] Canary/E2E `channel` e `person` concluído.
- [ ] PR ORCH integrado e rollout validado.
- [x] Documentação e evidências parciais atualizadas.
- [ ] Item marcado como concluído no estado geral.

Evidência parcial: a sessão `7477` do flow canário `c114383d-72e1-4401-8877-765e5bfac27f`, em `channel`, selecionou o membro `10768`, preservou o endereço da sessão, seguiu `selected`, terminou em `finish_flow` sem alarme e recebeu `200/status=received` do `api-bin`. A sessão local controlada `7480` repetiu o caminho com `session_scope=person`, `person_uuid` válido e o membro `10769`, também sem alarme e com confirmação externa. Como cada pessoa do mailing possui apenas um canal, a troca efetiva entre membros foi validada no teste PostgreSQL isolado. O rollout da cardinalidade `person` gerada pelo Target permanece pendente até integração e deploy da engine no ORCH; somente então o flow será adicionado à allowlist do Target Core.

Rollback: remover primeiro o UUID da allowlist `person` do Target Core e reiniciar seus produtores; depois interromper novas execuções antes de reverter a engine. Não há migration. Sessões já posicionadas no card seriam tratadas como componente não suportado pelo código anterior.

## Backlog avancado

Nao iniciar antes da revisao explicita posterior a conclusao dos itens comuns:

- `parallel_split` e `parallel_join`;
- `for_each`;
- `event_gateway`;
- `human_task`;
- `decision_table`;
- `compensation_scope`;
- `adaptive_split`;
- `agentic_task`.

Estas capacidades permanecem candidatas, nao compromissos do Alpha.
