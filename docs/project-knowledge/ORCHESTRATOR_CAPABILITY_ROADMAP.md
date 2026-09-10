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
- Usar flows reais somente como cenários E2E de composição. Nenhum cliente, campanha ou desenho específico deve virar regra de produção dentro de um card genérico.
- Reavaliar os componentes avancados somente depois da lista comum.

## Estado geral

| Ordem | Capacidade | Classificacao Alpha | Estado |
|---:|---|---|---|
| 0 | Fixacao da revisao por sessao | `ALPHA_FIX_OPTIONAL` | Concluido |
| 1 | `create_contact` | `ALPHA_FIX_OPTIONAL` | Concluido |
| 2 | `source_list_membership` | `ALPHA_FIX_OPTIONAL` | Concluído |
| 3 | `wait_for_event` | `ALPHA_FIX_OPTIONAL` | Concluído |
| 4 | `split_random` | `ALPHA_FIX_OPTIONAL` | Concluído |
| 5 | `select_contact_channel` | `ALPHA_FIX_OPTIONAL` | Concluído |
| 6 | `send_with_sms` | `ALPHA_FIX_OPTIONAL` | Concluído (marker-only) |
| 7 | `send_with_rcs` | `ALPHA_FIX_OPTIONAL` | Concluído (marker-only) |
| 8 | `send_with_email` | `ALPHA_FIX_OPTIONAL` | Em desenvolvimento |
| 9 | `fail_flow` | A classificar no desenho do envelope | Planejado |
| F1 | `send_with_dialer_handoff` para o fluxo integrado | `ALPHA_FIX_OPTIONAL` | Em desenvolvimento |

## Checklist padrao por card

Aplicar este checklist separadamente a cada item de 1 a 9:

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
- [x] Canary/E2E `channel` e `person` concluído.
- [x] PR ORCH integrado e rollout validado.
- [x] Documentação e evidências atualizadas.
- [x] Item marcado como concluído no estado geral.

Evidência concluída: a sessão `7477` do flow canário `c114383d-72e1-4401-8877-765e5bfac27f`, em `channel`, selecionou o membro `10768`, preservou o endereço da sessão, seguiu `selected`, terminou em `finish_flow` sem alarme e recebeu `200/status=received` do `api-bin`. A sessão local controlada `7480` repetiu o caminho com `session_scope=person`, `person_uuid` válido e o membro `10769`, também sem alarme e com confirmação externa. A PR ORCH `#154`, commit funcional `a27e110` e merge `ed3d84f`, foi implantada nos hosts `10.1.20.136` e `10.1.20.237` com health Celery `200`.

Depois do deploy, o flow entrou em `ORCHESTRATOR_PERSON_SCOPE_FLOW_UUIDS` nos hosts Target Core `10.1.20.128`, `10.1.20.129`, `10.1.20.239` e `10.1.20.249`. As APIs `full`/`crud` foram reiniciadas de forma rolling nos dois hosts de API, carregaram a nova allowlist em seus processos e mantiveram health `200`, sem erro no journal. O desvínculo e novo vínculo do mailing de teste pela rota oficial retornaram `200`; as duas pessoas produziram exatamente as sessões `7513` e `7514`, duas entidades e duas pessoas distintas, ambas em `session_scope=person`, `selected`, canal `voice` primário, `api_call=200`, `state=3` e zero erro. A auditoria tardia permaneceu com duas sessões, sem duplicação. O destino confirmou os dois POSTs pelos `stream_id` `1351296` e `1351297`.

Rollback: remover primeiro o UUID da allowlist `person` do Target Core e reiniciar seus produtores; depois interromper novas execuções antes de reverter a engine. Não há migration. Sessões já posicionadas no card seriam tratadas como componente não suportado pelo código anterior.

Próximo item: `send_with_sms`.

## Item 6 — `send_with_sms`

Objetivo: preparar o handoff de uma sessão para o canal SMS usando o membro selecionado explicitamente, preservando a autoridade do ORCH sobre a execução do flow e sem realizar o envio ao provedor na primeira entrega.

### Primeira entrega — handoff sem envio real

- o card aceita a configuração necessária ao futuro SMS, mas o ORCH não executa chamada HTTP ao provedor;
- número e membro são derivados de um canal telefônico elegível (`sms`, `phone` ou `voice`); essa compatibilidade expressa capacidade de SMS e não altera o tipo de origem;
- em escopo `person`, exige seleção compatível com SMS produzida por `select_contact_channel` dentro da mesma pessoa, lista e mailing;
- em escopo `channel`, preserva o membro e o endereço que originaram a sessão e valida que o canal é telefônico;
- o próprio ORCH grava `linked_actuator=sms` no `contact_list_member` exato e mantém a sessão bloqueada aguardando a integração externa e seus callbacks;
- nenhuma credencial, token, mensagem renderizada ou dado pessoal deve aparecer em log, alarme ou métrica;
- testes da primeira entrega devem provar que nenhum POST de SMS ocorre.

### Invariante para a ativação futura do envio real

`linked_actuator=sms` é somente a marca de roteamento. Isoladamente, ela não contém mensagem renderizada, revisão do flow, configuração completa do provedor, callbacks, chave de idempotência nem credencial segura. Portanto, um emissor não pode considerar a linha pronta para envio apenas porque encontrou essa marca.

Antes de habilitar o POST real, uma mudança separada deve obrigatoriamente:

- definir explicitamente qual serviço é responsável pelo dispatch e pelo tratamento de DLR, MO e status;
- materializar, sob autoridade do ORCH e a partir da revisão fixada da sessão, um `outbound_sms` ou contrato equivalente contendo o payload final de dispatch;
- vincular o envelope à sessão, ao card, ao membro exato e à revisão executada;
- incluir chave de idempotência e estados de claim/entrega que suportem retry sem duplicação silenciosa;
- guardar segredo como credencial protegida ou referência resolvível pelo emissor, nunca em logs ou respostas de diagnóstico;
- fazer o Supplier/emissor selecionar somente itens cujo envelope materializado esteja completo e pronto;
- normalizar os callbacks do provedor em um adaptador com correlação inequívoca antes de retomar a sessão;
- validar E2E fora da sandbox, observando o POST no destino, o retorno do provedor, os callbacks e a continuação correta do flow.

É proibido resolver a etapa futura fazendo Supplier/Target carregar a definição corrente do flow para descobrir mensagem, credencial ou callbacks. Isso quebraria a revisão fixada e devolveria ao consumidor uma responsabilidade de execução que pertence ao ORCH.

### Checklist específico do item 6

- [x] Confirmar os campos e branches do envelope no Target Core.
- [x] Garantir `422` para combinações estruturalmente inconsistentes.
- [x] Implementar o handoff marker-only no ORCH, sem chamada HTTP.
- [x] Validar o membro telefônico exato em `channel` e a seleção explícita compatível com SMS em `person`.
- [x] Provar por teste que `linked_actuator=sms` e o bloqueio são atômicos e idempotentes.
- [x] Provar por teste que nenhum cliente HTTP de SMS é invocado nesta fase.
- [x] Validar canários separados nos escopos `channel` e `person`.
- [x] Manter o envio real desabilitado até todas as pré-condições da seção anterior estarem implementadas em mudança própria.

Evidência local em 2026-09-07: o catálogo e o `422` já estavam integrados no Target Core, e o flow `f890dfa3-0657-4655-8a88-2ed7ae815e21` publicou `select_contact_channel(sms) -> send_with_sms` na revisão v1 `fef62654-c99b-4a4e-a364-7f51537d1b65`. A engine foi preparada no branch ORCH `feat/send-with-sms-runtime`, criado do `origin/main` `09cf2d1`. Os testes focados passaram em `150 passed`; dois testes PostgreSQL fora da sandbox comprovaram os guards e o rollback transacional. A suíte completa ficou em `595 passed, 27 failed`, nas mesmas famílias da baseline documentada. A stack `f5_local` permaneceu com API, três workers e dois Beats ativos, e o smoke encadeado aceitou as sessões `7527`/`7528`.

O primeiro vínculo posterior materializou membros telefônicos como `voice`, conforme o contrato real do domínio. As sessões `7529`/`7530` não alcançaram o card porque a seleção inicial exigia `sms` literal; isso confirmou que tipo de origem e decisão de atuação não podem ser confundidos. A correção de 2026-09-08 faz `select_contact_channel(sms)` aceitar `sms|phone|voice`, preserva o tipo real no contexto e deixa somente o ORCH gravar `linked_actuator=sms`. Os canários específicos após integração permanecem pendentes; em `person`, o flow ainda deverá entrar na allowlist do Target Core e selecionar explicitamente o membro antes do handoff.

Validação da correção: `48` testes focados e `2` testes PostgreSQL passaram; a suíte completa ficou em `599 passed, 28 failed`, com as 27 falhas da baseline e uma invalidação transitória de prepared statement que passou isoladamente. A stack `f5_local` encerrou os dois smokes nas sessões `7543`–`7552`, todas em `state=3`, cursor nulo e zero alarmes.

Rollback da primeira entrega: interromper novos usos do card, resolver ou terminalizar de forma auditada as sessões ainda bloqueadas nele e reverter catálogo/engine. Não há envio externo nem efeito remoto para compensar nessa fase. Alterações de `linked_actuator` já consumidas por sistemas externos devem ser auditadas antes de qualquer restauração de dados.

## Item 7 — `send_with_rcs`

Objetivo: preparar um handoff RCS somente quando o mailing materializou capacidade explícita, mantendo a autoridade de execução no ORCH e sem realizar envio ao provedor nesta primeira entrega.

Contrato concluído na primeira entrega marker-only:

- catálogo provider-neutral com `message_template` obrigatório e sem destinatário, endpoint ou credencial inventada;
- `select_contact_channel` aceita `rcs` por correspondência exata;
- `voice`, `phone`, `sms`, `whatsapp` e `email` não são considerados capacidade RCS;
- em `person`, seleção RCS anterior é obrigatória; em `channel`, o membro/endereço de origem é preservado;
- o ORCH grava `linked_actuator=rcs` no membro exato e bloqueia a sessão em `state=1` na mesma transação;
- nenhuma mensagem, endereço ou configuração do card é copiada para runtime, logs, alarmes ou métricas;
- nenhum cliente HTTP é chamado.

- [x] Semântica, efeitos e branches definidos.
- [x] Envelope atual definido no catálogo, sem versões paralelas.
- [x] Branches Target Core e ORCH criadas de `origin/main` atualizado.
- [x] Catálogo e validação `422` implementados no Target Core.
- [x] Testes focados do contrato Target Core aprovados.
- [x] Engine ORCH marker-only implementada com guards de membro exato.
- [x] Testes unitários provam bloqueio, idempotência, escopo `person`, rejeição de não-RCS, alarme e ausência de HTTP.
- [x] Teste PostgreSQL real concluiu guards, rollback e idempotência, com SMS como controle de regressão (`2 passed`).
- [x] Stack local completa reiniciada e smokes encadeados aprovados nas sessões `7557`–`7566`, todas encerradas sem cursor pendente nem alarmes.
- [x] Catálogo integrado e ambiente Target Core atualizado.
- [x] Flow canário publicado com `select_contact_channel(rcs) -> send_with_rcs`.
- [x] Canários `channel` e `person` concluídos sem envio externo.
- [x] PRs integradas e rollout validado.

Ativação futura de RCS real permanece proibida por R34 até existir API confirmada, envelope materializado, credencial protegida, idempotência e callbacks normalizados.

Rollback: impedir novos usos, auditar sessões bloqueadas e marcadores eventualmente consumidos, reverter catálogo/engine e reiniciar os serviços afetados. Não há migration nem POST externo para compensar.

## Item 8 — `send_with_email`

Objetivo: preparar o handoff de um membro explicitamente tipado como e-mail, com envelope visual alinhado ao núcleo comum de ferramentas de mercado e sem realizar envio nesta primeira entrega.

Contrato em desenvolvimento:

- envelope provider-neutral inspirado nos campos comuns de n8n, Amazon SES e SendGrid;
- remetente obrigatório; nome e reply-to opcionais; assunto obrigatório;
- corpo em `text`, `html` ou `both`, com validação condicional das versões;
- destinatário nunca configurado no card: vem do membro selecionado pelo ORCH;
- eventos positivos configuráveis `sent|delivered|opened|clicked` e timeout finito;
- branches provider-neutral `sent`, `delivered`, `opened`, `clicked`, `deferred`, `bounced`, `complained`, `unsubscribed`, `failed`, `timeout` e `exception`;
- em `person`, exige `select_contact_channel(email)` anterior; em `channel`, preserva o membro/endereço de origem;
- o ORCH grava somente `linked_actuator=email`, mantém a sessão em `state=1` e não chama HTTP/SMTP;
- configurações e conteúdo não são copiados para runtime, logs, alarmes ou métricas;
- branches são contrato visual futuro e não processam callbacks nesta entrega.

- [x] Semântica, configurações e branches definidos a partir de referências de mercado.
- [x] Branch Target Core criada do `origin/main` atualizado.
- [x] Catálogo e validação `422` implementados no Target Core.
- [x] Testes de catálogo, validação e create/update/publish aprovados.
- [ ] PR Target Core integrada e ambiente alvo atualizado.
- [x] Branch ORCH criada do `origin/main` atualizado.
- [x] Engine marker-only implementada com guard do membro `email` exato.
- [x] Testes unitários provam bloqueio, idempotência, `person|channel`, falha determinística e ausência de HTTP.
- [x] Teste PostgreSQL real comprova rollback transacional e isolamento por pessoa/lista/mailing/tipo.
- [x] Stack local completa reiniciada e smokes encadeados aprovados nas sessões `7677`–`7686`,
  todas em `state=3`, cursor final e zero alarmes.
- [ ] Flow canário publicado e validado primeiro em `channel`, depois em `person` quando aplicável.
- [ ] PR ORCH integrada e rollout validado.
- [ ] Documentação e evidências finais consolidadas.

Ativação futura do envio e das branches permanece proibida por R36 até existir envelope materializado ligado à revisão, credencial protegida, idempotência, claim/ACK/retry e normalização inequívoca dos eventos do provedor.

Rollback: impedir novos usos, auditar sessões bloqueadas e marcadores eventualmente consumidos, reverter catálogo/engine e reiniciar os serviços afetados. Não há migration nem envio externo para compensar.

## Objetivo integrador — fluxo `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`

Objetivo: usar um blueprint real para comprovar a composição dos componentes básicos, corrigindo ou acrescentando somente capacidades genéricas que faltarem. O flow é cenário de aceitação; nenhuma regra específica do cliente deve entrar na engine.

### Adaptação atual: `send_with_dialer_handoff`

- [x] Comparar o requisito com `send_with_dialer`, `run_flow`, `live` e `wait_for_event` existentes.
- [x] Preservar integralmente o `send_with_dialer` de produção.
- [x] Definir um novo card com destino pós-atendimento `bot|human` e configuração condicional equivalente aos cards já consolidados.
- [x] Preparar catálogo, `422` e envelope de voz no Target Core em branch isolada.
- [x] Preparar engine marker-only, seleção `person|channel`, retomada Dialer e composição com `wait_for_event` no ORCH em branch isolada.
- [x] Cobrir a corrida `answered`/tabulação sem mudar o comportamento genérico das demais esperas.
- [ ] Integrar e implantar os patches Target Core e ORCH.
- [ ] Adaptar o consumidor externo do envelope de voz para o destino humano, preservando o caminho BOT e o Dialer legado.
- [ ] Inserir o card no flow integrado e configurar os dois destinos em cenários controlados.
- [ ] Validar E2E: marcador, chamada atendida, BOT ou fila humana, callback `tabulation`, condição `positive|neutral|negative` e continuação até estado terminal.
- [ ] Retomar a montagem do restante do blueprint a partir do ponto comprovado.

Limite atual: o Target Core e o ORCH podem produzir e executar seus lados do contrato, mas o modo humano não deve ser declarado pronto antes da mudança no consumidor de voz e de um canário PBX. Consulte R37.

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
