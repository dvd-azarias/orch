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
- Em testes com Celery, evitar fila global compartilhada:
  - usar filas dedicadas do `orch` (`orch_dispatch`, `orch_execute`);
  - usar `CELERY_DISPATCH_WORKSPACE_UUID` quando o escopo for um workspace especifico;
  - quando necessario, usar `CELERY_BEAT_DISPATCH_ENABLED=false` para impedir dispatch global.

## Referencias operacionais

- `README.md`
- `docs/MIGRATIONS_PLAYBOOK.md`
