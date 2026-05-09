# AGENTS.md

Instrucoes operacionais para agentes neste repositorio `orch`.
Escopo: toda a arvore do projeto.

## Principios gerais

- Priorizar mudancas pequenas, cirurgicas e verificaveis.
- Nunca assumir sucesso sem validacao executavel.
- Em tarefas longas, reportar progresso de forma objetiva e frequente.
- Se houver bloqueio de ambiente, registrar causa e proximo passo imediatamente.

## Fonte da verdade por tipo de informacao

- `AGENTS.md`: regras estaveis de colaboracao e operacao.
- `README.md`: contexto funcional, rotas, fases e checklists de entrega.
- `docs/*.md`: playbooks e runbooks detalhados de operacao/migracao.

## Validacao minima antes de encerrar trabalho tecnico

1. Verificar servicos essenciais da stack quando a mudanca afeta runtime.
2. Executar validacao funcional real (ex.: chamadas HTTP com `curl`) quando aplicavel.
3. Executar testes automatizados relevantes (`pytest`) para regressao.
4. Consolidar evidencias objetivas (status, saida, logs relevantes).

## ATENCAO OBRIGATORIA SOBRE SANDBOX (DESTAQUE)

- TESTES QUE DEPENDEM DE REDE/DB/API EXTERNA DEVEM SER EXECUTADOS FORA DA SANDBOX.
- QUANDO HOUVER BLOQUEIO DE SANDBOX, O AGENTE DEVE PEDIR ELEVACAO IMEDIATAMENTE.
- SEM ELEVACAO E SEM EVIDENCIA EXTERNA, NAO DECLARAR TESTE COMO VALIDADO.
- PARA FLUXOS COM `api_call`, A VALIDACAO SO CONTA QUANDO O POST EXTERNO FOR OBSERVADO NO DESTINO.

## Regras de seguranca para Git

Sem confirmacao explicita do usuario, nao executar:

- criacao de branch
- commit
- push
- merge/rebase
- tag/release
- reset/revert

## Convencoes praticas deste repositorio

- Preferir comandos oficiais da aplicacao para migration:
  - `python -m app.cli migrate-all`
  - `python -m app.cli migrate-workspace <workspace_uuid>`
- Evitar procedimentos ad-hoc quando houver playbook documentado.
- Quando houver divergencia entre execucao e documentacao, atualizar a documentacao no mesmo trabalho.
- Regra padrao de filas (OBRIGATORIA): a menos que o usuario solicite explicitamente, NUNCA reutilizar nomes de filas que ja existam em outras aplicacoes/servicos do ambiente compartilhado.
  - Para novos fluxos/componentes/testes locais, criar filas dedicadas com sufixo/prefixo claro de isolamento (ex.: `*_f5_local`, `*_dev_<feature>`).
  - Objetivo: evitar impacto cruzado, backlog/ruidos de terceiros e diagnostico confuso no Celery Flower.
- Em testes com Celery, evitar fila global compartilhada:
  - usar filas dedicadas e isoladas por contexto (ex.: `orch_dispatch_f5_local`, `orch_execute_f5_local`);
  - usar `CELERY_DISPATCH_WORKSPACE_UUID` quando o escopo for um workspace especifico;
  - quando necessario, usar `CELERY_BEAT_DISPATCH_ENABLED=false` para impedir dispatch global.
- Regra de progressao entre fases (OBRIGATORIA):
  - fases novas devem ser validadas com a stack das fases anteriores em execucao;
  - antes de declarar regressao, repetir a subida padronizada e checar `status`;
  - para DEV local, usar `scripts/dev_phase_stack.sh` (`start`, `status`, `smoke`, `stop`) como sequencia canonical de retomada;
  - nao considerar fase validada sem passar pelo smoke encadeado (fluxo A + fluxo B no workspace alvo).
- Regra de manutencao de servicos (OBRIGATORIA):
  - apos intervencao em codigo/config da API, workers, beats ou filas, reiniciar os servicos antes de testar;
  - em macOS, usar `scripts/dev_phase_stack.sh restart` como padrao durante desenvolvimento;
  - `launchd` so deve ser usado quando solicitado explicitamente pelo usuario;
  - em qualquer fase futura (F6+), manter as fases homologadas anteriores ativas durante validacao real.

## Referencias operacionais

- `README.md`
- `docs/MIGRATIONS_PLAYBOOK.md`
