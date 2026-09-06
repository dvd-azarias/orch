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
| 0 | Fixacao da revisao por sessao | `ALPHA_FIX_OPTIONAL` | Em implementacao |
| 1 | `create_contact` | `ALPHA_FIX_OPTIONAL` | Concluido |
| 2 | `source_list_membership` | A classificar no desenho do envelope | Planejado |
| 3 | `wait_for_event` | A classificar no desenho do envelope | Planejado |
| 4 | `split_random` | A classificar no desenho do envelope | Planejado |
| 5 | `select_contact_channel` | A classificar no desenho do envelope | Planejado |
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

Branch ORCH: `fix/pin-session-flow-revision`.

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
- [ ] Executar canario controlado com uma sessao pausada entre duas publicacoes.
- [x] Atualizar R7 em `KNOWN_RISKS.md`, `DATA_FLOW.md` e o registro da manutencao.
- [ ] Revisao final e estrategia de rollback aprovadas.

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
