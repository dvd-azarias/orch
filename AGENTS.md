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

- Gatilho operacional (OBRIGATORIO):
  - quando o usuario escrever `SUBA_O_AMBIENTE`, interpretar como ordem para:
    1. subir toda a stack local homologada (API + workers + beats de todas as fases ja concluidas);
    2. validar status dos processos e filas principais;
    3. executar um smoke rapido de runtime (health + 1 curl real no workspace de teste);
    4. reportar pronto para testes manuais.
  - padrao de execucao:
    - preferencial: `scripts/dev_phase_stack.sh restart && scripts/dev_phase_stack.sh status`;
    - quando solicitado explicitamente pelo usuario: `scripts/launchd_orch.sh restart && scripts/launchd_orch.sh status`.
  - apos qualquer mudanca em codigo de runtime (API/Celery/filas), repetir `SUBA_O_AMBIENTE` antes de concluir regressao.

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
- Regra padrao de hostname Celery (OBRIGATORIA):
  - sempre subir workers com `--hostname` explicito para facilitar filtro no Flower;
  - padrao DEV local (macOS): sufixo `@_macbook_deivid_dev`;
  - padrao servidor `10.1.20.136`: sufixo `@136_01`;
  - exemplos: `orch-celery-worker@...`, `orch-celery-fileapp-worker@...`, `orch-celery-generate-file-worker@...`.
- Regra de perfil de filas (OBRIGATORIA):
  - usar `ORCH_QUEUE_PROFILE` como chave primaria para selecao de filas por ambiente;
  - `auto` (padrao): macOS -> `launchd_local`; Linux -> `prod`;
  - perfis aceitos: `launchd_local`, `f5_local`, `prod`;
  - evitar hardcode manual recorrente de filas no `.env`; usar override `CELERY_*_QUEUE` somente quando necessario.
- Regra de progressao entre fases (OBRIGATORIA):
  - fases novas devem ser validadas com a stack das fases anteriores em execucao;
  - antes de declarar regressao, repetir a subida padronizada e checar `status`;
  - para DEV local, usar `scripts/dev_phase_stack.sh` (`start`, `status`, `smoke`, `stop`) como sequencia canonical de retomada;
  - nao considerar fase validada sem passar pelo smoke encadeado (fluxo A + fluxo B no workspace alvo).
- Regra de manutencao de servicos (OBRIGATORIA):
  - apos intervencao em codigo/config da API, workers, beats ou filas, reiniciar os servicos antes de testar;
  - em macOS, usar `scripts/dev_phase_stack.sh restart` como padrao durante desenvolvimento;
  - `launchd` so deve ser usado quando solicitado explicitamente pelo usuario;
  - durante desenvolvimento, depuracao e ajuste fino, subir SEMPRE todos os processos das fases ja homologadas (API + workers + beats) antes de validar;
  - nao usar servidor remoto como ambiente de debug continuo; usar servidor apenas para marco de homologacao/release;
  - evitar ciclo \"erro pequeno -> deploy servidor\"; corrigir e validar primeiro na stack local completa;
  - em qualquer fase futura (F6+), manter as fases homologadas anteriores ativas durante validacao real.
  - NUNCA misturar `launchd` e stack manual (`scripts/dev_phase_stack.sh`/TTY) ao mesmo tempo.
  - Antes de validar E2E, confirmar quem esta na porta `7777` e quais workers/beats estao ativos para evitar processo com codigo antigo.
  - Se houver conflito/duvida de processo stale, parar tudo e subir novamente de forma limpa (fonte unica de execucao).

## Regra canonica FileApp (Fase 7) — OBRIGATORIA

- Decisao por `mapping_template` e somente por ele:
  - `tipo_1` (com `mapping_template`): deve persistir em `persons` E em `orch_sessions`.
  - `tipo_2` (sem `mapping_template`): deve persistir somente em `orch_sessions`.
- Nao criar rota nova para FileApp; usar a rota oficial:
  - `POST /v1/orch/{workspace_uuid}/{flow_uuid}`.
- Evidencia minima obrigatoria em testes E2E de FileApp:
  1. resposta `202 accepted` contendo `pipeline` (`fileapp_tipo1_ingest` ou `fileapp_tipo2_ingest`);
  2. task de ingest recebida/enfileirada no worker;
  3. task de processamento concluida no worker;
  4. query SQL comprovando persistencia conforme tipo (`persons` + `orch_sessions` ou somente `orch_sessions`).
- Em diagnostico, sempre registrar no log o tipo decidido:
  - `decision=fileapp_tipo1` ou `decision=fileapp_tipo2`.

## Regra complementar FileApp (Fase 10) — OBRIGATORIA

- No caminho `tipo_1` (com `mapping_template`), executar etapa de associacao de mailing ao flow.
- Chamada esperada:
  - `POST {SYNC_WEBHOOK_BASE_URL}/v2/flow/{flow_uuid}/mailings`
- Body obrigatorio:
  - `mailing_ids_added` com o mailing resolvido do template;
  - `mailing_ids_removed` vazio;
  - `linked_by` = `file.id` do evento;
  - `call_origin` = **`file_event`** (sempre).
- Resolver mailing via:
  - `source_list_mapping_templates.created_from_source_list_id` -> `source_lists.public_id`.

## Referencias operacionais

- `README.md`
- `docs/MIGRATIONS_PLAYBOOK.md`
