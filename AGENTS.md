# AGENTS.md

Instrucoes operacionais para agentes neste repositorio `orch`.

Escopo: toda a arvore do projeto.

Este sistema encontra-se em producao e possui carater de ALPHA em sustentacao enquanto sua V2 e desenvolvida separadamente.

O objetivo deste repositorio, neste momento, e:

> manter o ORCH estavel, previsivel, diagnosticavel e seguro ate sua substituicao pela V2.

## Principios gerais

* Priorizar mudancas pequenas, cirurgicas e verificaveis.
* Priorizar estabilidade de producao sobre elegancia arquitetural.
* Nunca assumir sucesso sem validacao executavel.
* Evitar refatoracao oportunista durante correcoes.
* Nao modernizar arquitetura, dependencias ou estrutura sem necessidade operacional concreta.
* Preservar contratos e comportamentos existentes por padrao.
* Em tarefas longas, reportar progresso de forma objetiva e frequente.
* Se houver bloqueio de ambiente, registrar causa e proximo passo imediatamente.
* Quando uma mudanca estrutural puder esperar pela V2 sem risco operacional, preferir a V2.
* Toda manutencao relevante deve aumentar, quando aplicavel, nao apenas a estabilidade do sistema, mas tambem o conhecimento documentado sobre ele.

## Stewardship do projeto

Este repositorio utiliza um modelo de sustentacao baseado em um `Project Steward`.

Antes de realizar investigacao relevante, manutencao, correcao de bug, diagnostico de incidente ou alteracao de runtime, ler:

* `PROJECT_STEWARD.md`
* `PROJECT_BRAIN.md`, quando existir
* documentacao relevante em `docs/project-knowledge/`, quando existir
* `README.md`
* playbooks/runbooks relacionados a area afetada

`PROJECT_STEWARD.md` define:

* filosofia de manutencao deste Alpha;
* processo de investigacao;
* classificacao Alpha x V2;
* analise de risco e blast radius;
* uso de agentes auxiliares;
* revisao independente;
* politica de documentacao e memoria do projeto.

As regras operacionais deste `AGENTS.md` continuam obrigatorias.

O `PROJECT_STEWARD.md` complementa este arquivo; nao o substitui.

## Regra de conhecimento persistente

Nao depender apenas da memoria da thread, sessao ou agente.

Conhecimento importante descoberto durante manutencoes e investigacoes deve ser persistido no repositorio quando trouxer valor futuro.

Estrutura preferencial:

* `PROJECT_BRAIN.md`: mapa executivo e operacional do conhecimento do projeto.
* `docs/project-knowledge/ARCHITECTURE.md`: arquitetura real.
* `docs/project-knowledge/COMPONENTS.md`: componentes e responsabilidades.
* `docs/project-knowledge/DATA_FLOW.md`: fluxos importantes.
* `docs/project-knowledge/DATABASE.md`: persistencia e dados.
* `docs/project-knowledge/DEPENDENCIES.md`: dependencias relevantes.
* `docs/project-knowledge/EXTERNAL_INTEGRATIONS.md`: integracoes externas.
* `docs/project-knowledge/CONFIGURATION.md`: configuracoes relevantes.
* `docs/project-knowledge/PRODUCTION_RUNBOOK.md`: diagnostico e operacao.
* `docs/project-knowledge/KNOWN_QUIRKS.md`: comportamentos estranhos/intencionais.
* `docs/project-knowledge/KNOWN_RISKS.md`: riscos conhecidos.
* `docs/project-knowledge/TECHNICAL_DEBT.md`: divida tecnica classificada.
* `docs/project-knowledge/INCIDENT_HISTORY.md`: memoria de incidentes relevantes.
* `docs/project-knowledge/MAINTENANCE_LOG.md`: contexto de manutencoes relevantes.
* `docs/project-knowledge/GLOSSARY.md`: conceitos especificos quando necessario.

Nao criar documentacao vazia apenas para cumprir estrutura.

Criar e atualizar arquivos conforme houver conhecimento real.

## Fonte da verdade por tipo de informacao

* `AGENTS.md`: regras estaveis de colaboracao e operacao.
* `PROJECT_STEWARD.md`: estrategia e processo de sustentacao do Alpha.
* `PROJECT_BRAIN.md`: mapa consolidado do conhecimento atualmente confirmado sobre o projeto.
* `README.md`: contexto funcional, rotas, fases e checklists de entrega.
* `docs/project-knowledge/*.md`: memoria tecnica e operacional acumulada.
* demais `docs/*.md`: playbooks e runbooks detalhados de operacao/migracao.
* codigo executado, testes e configuracao: evidencia do comportamento real.

Quando houver divergencia entre documentacao e comportamento real:

1. investigar;
2. determinar qual comportamento realmente ocorre;
3. nao alterar codigo apenas para satisfazer documentacao antiga;
4. atualizar a documentacao quando apropriado;
5. registrar divergencias relevantes.

## Politica para manutencao Alpha

Para alteracoes de sustentacao, seguir por padrao:

> MINIMUM SAFE CHANGE

Antes de alterar codigo relevante, determinar:

* causa ou hipotese da causa;
* menor ponto seguro de alteracao;
* arquivos realmente necessarios;
* contratos que precisam permanecer inalterados;
* blast radius;
* impacto em runtime;
* forma de validacao;
* estrategia de rollback.

Evitar:

* limpeza de codigo sem relacao com a tarefa;
* renomeacoes amplas;
* reorganizacao de modulos;
* troca de bibliotecas sem necessidade;
* grandes abstracoes;
* refatoracoes estruturais;
* alteracoes arquiteturais que pertencem a V2.

Codigo feio, antigo ou pouco elegante nao e, isoladamente, um motivo para modifica-lo.

## Classificacao Alpha x V2

Quando relevante, classificar a mudanca como:

* `ALPHA_FIX_REQUIRED`
* `ALPHA_FIX_OPTIONAL`
* `V2_ONLY`

`ALPHA_FIX_REQUIRED`:

* bug de producao;
* indisponibilidade;
* risco de integridade;
* seguranca;
* impacto a clientes;
* problema operacional;
* falha de desempenho necessaria;
* problema de observabilidade que impede diagnostico importante.

`ALPHA_FIX_OPTIONAL`:

* mudanca pequena;
* baixo risco;
* beneficio operacional claro;
* reducao significativa de manutencao recorrente.

`V2_ONLY`:

* refatoracao estrutural;
* modernizacao;
* nova arquitetura;
* grande reorganizacao;
* substituicao de tecnologia;
* melhorias esteticas;
* divida tecnica sem impacto relevante no Alpha atual.

Nunca transformar silenciosamente o Alpha em uma segunda V2.

## Validacao minima antes de encerrar trabalho tecnico

1. Verificar servicos essenciais da stack quando a mudanca afeta runtime.
2. Executar validacao funcional real (ex.: chamadas HTTP com `curl`) quando aplicavel.
3. Executar testes automatizados relevantes (`pytest`) para regressao.
4. Consolidar evidencias objetivas (status, saida, logs relevantes).
5. Quando houver alteracao significativa, avaliar se `PROJECT_BRAIN.md` ou `docs/project-knowledge/` precisam ser atualizados.

Nao declarar uma alteracao como validada apenas porque:

* o codigo parece correto;
* testes estaticos passaram;
* import funcionou;
* funciona teoricamente;
* funciona apenas por leitura do codigo.

Quando houver comportamento de runtime envolvido, buscar evidencia de runtime.

## ATENCAO OBRIGATORIA SOBRE SANDBOX (DESTAQUE)

* TESTES QUE DEPENDEM DE REDE/DB/API EXTERNA DEVEM SER EXECUTADOS FORA DA SANDBOX.
* QUANDO HOUVER BLOQUEIO DE SANDBOX, O AGENTE DEVE PEDIR ELEVACAO IMEDIATAMENTE.
* SEM ELEVACAO E SEM EVIDENCIA EXTERNA, NAO DECLARAR TESTE COMO VALIDADO.
* PARA FLUXOS COM `api_call`, A VALIDACAO SO CONTA QUANDO O POST EXTERNO FOR OBSERVADO NO DESTINO.

## Regras de seguranca para Git

Sem confirmacao explicita do usuario, nao executar:

* criacao de branch
* commit
* push
* merge/rebase
* tag/release
* reset/revert

Tambem nao:

* alterar historico Git;
* apagar alteracoes do usuario;
* descartar arquivos modificados apenas para obter working tree limpa.

Git pode ser utilizado livremente para investigacao nao destrutiva, incluindo:

* `git status`
* `git diff`
* `git log`
* `git show`
* `git blame`

quando essas operacoes ajudarem a compreender comportamento, origem de decisoes ou regressao.

## Uso de agentes auxiliares

O Project Steward pode utilizar agentes/subagentes para investigacoes independentes quando isso aumentar a confiabilidade.

Especialidades previstas:

* Project Archaeologist
* Maintenance Engineer
* QA / Reviewer
* Incident / Production Analyst
* Documentation Keeper

Nao e obrigatorio utilizar todos os agentes em toda tarefa.

Mudancas pequenas e de baixo risco podem ser tratadas diretamente.

Mudancas de medio/alto risco devem preferencialmente ter:

1. investigacao;
2. implementacao;
3. revisao independente.

Sempre que possivel, o mesmo agente que implementou uma alteracao nao deve ser a unica fonte de validacao conceitual da propria solucao.

## Convencoes praticas deste repositorio

* Gatilho operacional (OBRIGATORIO):

  * quando o usuario escrever `SUBA_O_AMBIENTE`, interpretar como ordem para:

    1. subir toda a stack local homologada (API + workers + beats de todas as fases ja concluidas);
    2. validar status dos processos e filas principais;
    3. executar um smoke rapido de runtime (health + 1 curl real no workspace de teste);
    4. reportar pronto para testes manuais.
  * padrao de execucao:

    * preferencial: `scripts/dev_phase_stack.sh restart && scripts/dev_phase_stack.sh status`;
    * quando solicitado explicitamente pelo usuario: `scripts/launchd_orch.sh restart && scripts/launchd_orch.sh status`.
  * apos qualquer mudanca em codigo de runtime (API/Celery/filas), repetir `SUBA_O_AMBIENTE` antes de concluir regressao.

* Preferir comandos oficiais da aplicacao para migration:

  * `python -m app.cli migrate-all`
  * `python -m app.cli migrate-workspace <workspace_uuid>`

* Evitar procedimentos ad-hoc quando houver playbook documentado.

* Quando houver divergencia entre execucao e documentacao, atualizar a documentacao no mesmo trabalho.

* Regra padrao de filas (OBRIGATORIA): a menos que o usuario solicite explicitamente, NUNCA reutilizar nomes de filas que ja existam em outras aplicacoes/servicos do ambiente compartilhado.

  * Para novos fluxos/componentes/testes locais, criar filas dedicadas com sufixo/prefixo claro de isolamento (ex.: `*_f5_local`, `*_dev_<feature>`).
  * Objetivo: evitar impacto cruzado, backlog/ruidos de terceiros e diagnostico confuso no Celery Flower.

* Em testes com Celery, evitar fila global compartilhada:

  * usar filas dedicadas e isoladas por contexto (ex.: `orch_dispatch_f5_local`, `orch_execute_f5_local`);
  * usar `CELERY_DISPATCH_WORKSPACE_UUID` quando o escopo for um workspace especifico;
  * quando necessario, usar `CELERY_BEAT_DISPATCH_ENABLED=false` para impedir dispatch global.

* Regra padrao de hostname Celery (OBRIGATORIA):

  * sempre subir workers com `--hostname` explicito para facilitar filtro no Flower;
  * padrao DEV local (macOS): sufixo `@_macbook_deivid_dev`;
  * padrao servidor `10.1.20.136`: sufixo `@136_01`;
  * exemplos: `orch-celery-worker@...`, `orch-celery-fileapp-worker@...`, `orch-celery-generate-file-worker@...`.

* Regra de perfil de filas (OBRIGATORIA):

  * usar `ORCH_QUEUE_PROFILE` como chave primaria para selecao de filas por ambiente;
  * `auto` (padrao): macOS -> `launchd_local`; Linux -> `prod`;
  * perfis aceitos: `launchd_local`, `f5_local`, `prod`;
  * evitar hardcode manual recorrente de filas no `.env`; usar override `CELERY_*_QUEUE` somente quando necessario.

* Regra de progressao entre fases (OBRIGATORIA):

  * fases novas devem ser validadas com a stack das fases anteriores em execucao;
  * antes de declarar regressao, repetir a subida padronizada e checar `status`;
  * para DEV local, usar `scripts/dev_phase_stack.sh` (`start`, `status`, `smoke`, `stop`) como sequencia canonical de retomada;
  * nao considerar fase validada sem passar pelo smoke encadeado (fluxo A + fluxo B no workspace alvo).

* Regra de manutencao de servicos (OBRIGATORIA):

  * apos intervencao em codigo/config da API, workers, beats ou filas, reiniciar os servicos antes de testar;
  * em macOS, usar `scripts/dev_phase_stack.sh restart` como padrao durante desenvolvimento;
  * `launchd` so deve ser usado quando solicitado explicitamente pelo usuario;
  * durante desenvolvimento, depuracao e ajuste fino, subir SEMPRE todos os processos das fases ja homologadas (API + workers + beats) antes de validar;
  * nao usar servidor remoto como ambiente de debug continuo; usar servidor apenas para marco de homologacao/release;
  * evitar ciclo "erro pequeno -> deploy servidor"; corrigir e validar primeiro na stack local completa;
  * em qualquer fase futura (F6+), manter as fases homologadas anteriores ativas durante validacao real;
  * NUNCA misturar `launchd` e stack manual (`scripts/dev_phase_stack.sh`/TTY) ao mesmo tempo;
  * antes de validar E2E, confirmar quem esta na porta `7777` e quais workers/beats estao ativos para evitar processo com codigo antigo;
  * se houver conflito/duvida de processo stale, parar tudo e subir novamente de forma limpa (fonte unica de execucao).

## Regra canonica FileApp (Fase 7) — OBRIGATORIA

* Decisao por `mapping_template` e somente por ele:

  * `tipo_1` (com `mapping_template`): deve persistir em `persons` E em `orch_sessions`.
  * `tipo_2` (sem `mapping_template`): deve persistir somente em `orch_sessions`.
* Nao criar rota nova para FileApp; usar a rota oficial:

  * `POST /v1/orch/{workspace_uuid}/{flow_uuid}`.
* Evidencia minima obrigatoria em testes E2E de FileApp:

  1. resposta `202 accepted` contendo `pipeline` (`fileapp_tipo1_ingest` ou `fileapp_tipo2_ingest`);
  2. task de ingest recebida/enfileirada no worker;
  3. task de processamento concluida no worker;
  4. query SQL comprovando persistencia conforme tipo (`persons` + `orch_sessions` ou somente `orch_sessions`).
* Em diagnostico, sempre registrar no log o tipo decidido:

  * `decision=fileapp_tipo1` ou `decision=fileapp_tipo2`.

## Regra complementar FileApp (Fase 10) — OBRIGATORIA

* No caminho `tipo_1` (com `mapping_template`), executar etapa de associacao de mailing ao flow.
* A chamada de associacao deve ser assincrona via Celery (task dedicada), nunca bloquear o processamento local.
* Chamada esperada da task:

  * `POST {SYNC_WEBHOOK_BASE_URL}/v2/flow/{flow_uuid}/mailings`
* Body obrigatorio:

  * `mailing_ids_added` com o mailing resolvido do template;
  * `mailing_ids_removed` vazio;
  * `linked_by` = `file.id` do evento;
  * `call_origin` = **`file_event`** (sempre).
* Resolver mailing via:

  * criar `source_lists` no proprio ORCH (1 por `file.id`) e usar `source_lists.public_id`.
* Regra de status da source_list (OBRIGATORIA):

  * nao usar `source_lists.status = UPLOADED` para associacao de mailing;
  * enfileirar `associate_mailing` somente quando a `source_list` estiver em `READY_TO_INGEST`.
* Nao manipular `source_list_members` localmente neste fluxo.

## Referencias operacionais

* `PROJECT_STEWARD.md`
* `PROJECT_BRAIN.md`, quando existir
* `README.md`
* `docs/project-knowledge/`, quando existir
* `docs/MIGRATIONS_PLAYBOOK.md`
