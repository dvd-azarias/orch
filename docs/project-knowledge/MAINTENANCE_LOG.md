# Maintenance Log

## 2026-09-06 — Engine do card `split_random`

### REQUEST / CLASSIFICATION

Implementar no ORCH o contrato A/B já integrado ao catálogo do Target Core. `ALPHA_FIX_OPTIONAL`; risco baixo/médio por introduzir uma nova decisão de branch no executor, sem migration, tabela, endpoint, fila ou efeito externo próprio.

### CHANGE

- Normalizar os percentuais inteiros de A/B e a variável de saída no mesmo contrato aceito pelo Target Core.
- Gerar bucket `0..99` por SHA-256 de `flow + session + revision + card`; retry ou redelivery da mesma identidade produz a mesma variante.
- Validar exatamente uma saída `variant_a`, uma `variant_b` e no máximo uma `exception`, evitando o fallback legado para a primeira edge quando o grafo estiver inconsistente.
- Gravar a variante em `variables.customs[output_var]`, diagnóstico em `split_random_last_result` e falhas em `split_random_last_error`, sem persistir o material usado como seed.
- Usar `exception` para falhas controladas; sem branch válida, terminalizar uma vez pelos códigos `split_random_*` para impedir loop permanente.

### VALIDATION

- Testes diretos do card: `26 passed`; regressão focada de workflow, revisão e cards comuns: `181 passed`.
- Suíte completa fora da sandbox: `549 passed, 27 failed`. Vinte e seis falhas pertencem à família de baseline já documentada; a falha adicional de WhatsApp por estado/ordem reproduziu com o mesmo resultado na árvore limpa equivalente à `origin/main`.
- `py_compile` e `git diff --check` passaram.
- Stack completa reiniciada em terminal persistente com perfil e filas `f5_local`; API, três workers e dois Beats ficaram `up`. Os smokes encadeados criaram `7431` e `7432`, ambas `state=3`, cursor nulo e zero alarmes.
- Canary no flow `041c493a-b8cc-4deb-895c-8efa5f73e1bb`, revisão publicada v1 `06fdc5b7-d477-44bf-b055-b2eef1c6260a`: as sessões `7433`–`7440` terminaram em `state=3`, quatro por `variant_a` e quatro por `variant_b`, com zero alarmes. Todos os buckets persistidos coincidiram com o recálculo pela identidade da sessão.
- Cada sessão executou exatamente um `api_call` posterior e recebeu HTTP 200/`status=received` do API-bin em uma tentativa, com `stream_id` entre `1345978` e `1345987` para os oito resultados observados.
- Como o banco é compartilhado, o dispatcher de produção alcançou a sessão `7440` antes do worker local e registrou `component_not_supported:split_random`. Ele não avançou o cursor nem chamou o destino; em seguida a engine local escolheu `variant_b`, fez um único POST e finalizou. Os workers do host `237` foram confirmados apenas nas filas de produção, portanto não houve consumo cruzado de `f5_local`; a interferência ocorreu pela varredura do mesmo workspace no banco.
- A auditoria tardia após desligar a stack manteve estados, cursores, buckets, contagens de métricas e zero alarmes inalterados, sem sinal de hot loop.
- No encerramento, o script voltou a deixar subprocessos órfãos. Os seis mestres foram identificados pelo diretório do worktree e filas locais, encerrados explicitamente, e a auditoria final confirmou porta `7777` livre e ausência de Uvicorn/Celery locais.

### POST-DEPLOY

- A PR ORCH `#152` foi integrada em `main` pelo merge commit `8345284`. O rollout fez fast-forward nos hosts `10.1.20.136` e `10.1.20.237`; os checksums dos respectivos `.env` permaneceram idênticos antes e depois, com backups restritos em `.maintenance-backups/.env.pre-split-random-20260907T0203Z`.
- No `136` foi reiniciada somente a API. No `237`, a API e os workers de workflow `01`–`05` foram reiniciados de forma gradual; cada worker confirmou `ready`. FileApp, generate-file, billing e Beats não foram reiniciados.
- Após o rollout, API, banco, broker, workers e Beat ficaram saudáveis nos dois hosts, sem unidade ORCH falha ou erro novo no journal. Os cinco workers `orch-celery-worker@237_01`–`05` ficaram visíveis no health check.
- O canário de produção criou as sessões `7441`–`7448`: todas terminaram em `state=3`, três por `variant_a` e cinco por `variant_b`, com os oito buckets iguais ao recálculo determinístico. Cada sessão registrou exatamente um `split_random`, um `api_call` e um `finish_flow`.
- Os oito POSTs posteriores foram observados no API-bin com HTTP 200/`status=received` na primeira tentativa, `stream_id` `1346110`–`1346117`. Não houve alarme, `component_not_supported:split_random` ou erro `split_random_*`; a auditoria tardia manteve estados e contagens inalterados.

### RISK / ROLLBACK

O percentual representa amostragem determinística, não uma cota exata em lotes pequenos. O grafo inválido não é executado silenciosamente. Para rollback, impedir novos triggers do card, reverter a engine e reiniciar API/workers; não há migration ou dado externo do próprio card a desfazer. Sessões ainda posicionadas no card voltariam ao stop seguro `component_not_supported:split_random` caso fossem alcançadas somente por código anterior.

## 2026-09-06 — Terminalização determinística de `api_call_missing_url`

### REQUEST / CLASSIFICATION

Interromper a amplificação de erro permanente observada em duas sessões de produção sem processar o card incorretamente. `ALPHA_FIX_REQUIRED`; risco médio por alterar roteamento de exceção, terminalidade e seleção do reconciliador, sem migration, endpoint, fila ou efeito externo novo.

### CHANGE

- O executor do `api_call` passa a usar a branch `exception*` já definida no grafo quando a preparação lança `WorkflowExecutionError`, persistindo `api_call_last_error` sem registrar URL ou payload nos logs.
- `api_call_missing_url` entra nos conjuntos sincronizados de falha terminal da engine e do dispatcher. Sem branch de exceção, a sessão termina uma vez e o Celery persiste um único alarme.
- O repositório do reconciliador considera somente sessões `state IN (0,1,2)`, sem `ended_at` e sem `unassigned_at`. O filtro fica dentro da CTE, antes do `LIMIT`, para sessões terminais antigas não causarem starvation do lote.

### VALIDATION

- Testes focados de engine, dispatcher, task e repositório: `18 passed`.
- Regressão direcionada: `118 passed, 6 failed`; suíte completa: `524 passed, 26 failed`. Todas as falhas reproduzem a baseline legada de `trigger_orch(flow_uuid=...)`, exceto uma expectativa também preexistente de execução inline. Nenhum arquivo desses testes foi alterado pelo patch.
- `compileall` e `git diff --check` passaram; `ruff` não está instalado na `.venv` atual.
- Stack local completa em terminal persistente: API, três workers e dois Beats `up`; smoke canônico dos dois flows criou as sessões `7413`/`7414`, e os workers concluíram as tasks.
- Prova real assíncrona com PostgreSQL/RabbitMQ/Redis: a sessão `7415`, com branch `exception`, terminou em `state=3`, cursor nulo e `api_call_last_error`, sem falha terminal ou alarme. A sessão `7416`, sem branch, terminou em `state=3`, cursor nulo, `terminal_failure=api_call_missing_url` e exatamente um alarme.
- Foi inserido um evento WhatsApp pendente temporário na sessão terminal `7416`; a consulta real do reconciliador não a selecionou (`selected_by_reconciler=false`) e o evento foi removido.
- Ao final, a stack foi encerrada; como os wrappers dos PID files deixaram subprocessos órfãos, os seis processos-mestre exatos foram finalizados e uma checagem independente confirmou porta `7777` livre e ausência de workers/beats locais.

### RISK / ROLLBACK

O patch não resolve a variável ausente nem tenta executar uma chamada sem URL. Flows com branch de exceção passam a seguir o contrato já desenhado; flows sem branch deixam de repetir indefinidamente e exigem correção funcional. Rollback é apenas de código e restart dos workers/beat; não há migration. Sessões já terminalizadas não são reabertas automaticamente.

## 2026-09-06 — Engine do card `wait_for_event`

### REQUEST / CLASSIFICATION

Implementar no ORCH o contrato já publicado no catálogo do Target Core. `ALPHA_FIX_OPTIONAL`; risco médio por alterar bloqueio, dispatcher, callback e concorrência da sessão, sem migration, endpoint, fila ou efeito externo novo.

### CHANGE

- Armar uma espera finita no próprio cursor, com `state=0`, `frozen_until` e prazo persistido que não se renova em reentradas.
- Comparar `event_name/result` sem distinção de maiúsculas/minúsculas e consumir somente callback posterior ao baseline do card e recebido até o prazo.
- Preservar callbacks antigos, não correspondentes e tardios; emitir `received`, `timeout` ou `exception` e gravar saída em `variables.customs[output_var]`.
- Serializar callback e executor pelo advisory lock `92021/session_id`. Um callback correspondente remove o congelamento e mantém `state=0`, cobrindo a corrida conhecida de enqueue antes do commit.
- Registrar armamento/conclusão/erro e métricas sem colocar `entity` ou `data` nos logs.

### VALIDATION

- Testes focados de engine, dispatcher e repositório: `28 passed`.
- Regressão direcionada de workflow/callback/revisão/tasks: `185 passed, 1 failed`; a falha usa a assinatura legada `trigger_orch(flow_uuid=...)` já pertencente à baseline.
- PostgreSQL real fora da sandbox: `2 passed` para o novo ciclo callback→retomada e a regressão transacional de `source_list_membership`. A sessão de prova foi criada em transação revertida e a consulta posterior confirmou zero resíduo pelo UUID.
- Suíte completa final fora da sandbox: `517 passed, 28 failed`. As falhas de assinatura reproduzem a baseline legada; as duas falhas potencialmente afetadas por ordem/estado compartilhado passaram isoladamente logo depois.
- `py_compile` e `git diff --check` passaram. `ruff` não está instalado na `.venv` atual.
- Stack local completa reiniciada na branch com perfil `f5_local`; API, três workers e dois Beats ficaram `up` em TTY persistente.
- Smokes encadeados criaram as sessões `7366` e `7367`, ambas concluídas em `state=3` e com zero alarmes. Esses flows não possuem o card novo e comprovam regressão da stack, não o E2E de `wait_for_event`.
- O flow canário publicado foi configurado depois dessa primeira busca. Antes do rollout, execução transacional e canários locais confirmaram os caminhos `received` e `timeout`; a validação definitiva está registrada abaixo.

### POST-DEPLOY

- A PR ORCH `#149`, merge `ce4764164f28d4371cf8cc38b6efb89395a769c6`, foi implantada por fast-forward em `/etc/gohp/orch` nos hosts `10.1.20.136` e `10.1.20.237`.
- Os `.env` locais foram preservados e copiados para `.maintenance-backups/.env.pre-wait-for-event-20260906-2030`; os checksums permaneceram idênticos antes e depois do pull.
- No `237`, os cinco workers gerais foram reiniciados em rolling restart e anunciaram `237_01..05 ready`; o beat principal e a API também foram reiniciados. FileApp, generate-file e billing não foram interrompidos. No `136`, somente a API foi reiniciada, preservando a topologia sem workers.
- As duas APIs responderam HTTP 200 em `live`, `ready` e `celery`; os cinco workers gerais responderam `pong`, as units afetadas ficaram `active` e nenhuma unit ORCH ficou em `failed`.
- No flow `f7414852-e4fc-4e5f-8bb6-4e8ec2d317c8`, revisão publicada v3 `ab839afc-53fc-4830-8564-8d5dd4258f5e`, a sessão `7398` aguardou 10 segundos, seguiu pela branch `timeout`, executou o card de marcação com HTTP 200 (`stream_id=1345250`) e terminou em `state=3`, `frozen_until=NULL`, oito métricas e zero alarmes.
- A sessão `7399` foi armada e recebeu callback `BOLETO_PAGO` pela rota canônica; reutilizou a mesma sessão, consumiu o callback, seguiu pela branch `received`, confirmou HTTP 200 (`stream_id=1345252`) e terminou em `state=3`, `frozen_until=NULL`, oito métricas e zero alarmes.
- A auditoria tardia manteve ambas as sessões com oito métricas e `finished_by_component`, comprovando ausência do hot loop observado antes do rollout.
- O journal também mostrou execuções antigas e independentes falhando periodicamente com `api_call sem URL válida`, inclusive antes do restart. Elas não afetaram os canários e nenhuma unit ORCH caiu; o saneamento dessas sessões/flows permanece uma investigação separada.

### RISK / ROLLBACK

A correlação Alpha continua escolhendo uma sessão ativa por `flow_uuid + entity`; sessões paralelas com a mesma entidade permanecem ambíguas (R32). Para rollback, impedir novos usos e resolver/aguardar as esperas ativas antes de reverter a engine, pois o código anterior trataria o cursor ainda posicionado no card como componente não suportado. Não há migration ou efeito externo a desfazer.

## 2026-09-06 — Engine do card `source_list_membership`

### REQUEST / CLASSIFICATION

Implementar no ORCH o componente aditivo publicado no catálogo do Target Core. `ALPHA_FIX_OPTIONAL`; risco médio por escrita transacional em tabelas compartilhadas de pessoa e lista, sem migration, fila ou integração externa nova.

### CHANGE

- Resolver `person_uuid` literal ou por template e `mailing_id` pelo UUID público da lista.
- Criar ou reutilizar `contact_drafts`/`source_list_contact_drafts` com a rotina já comprovada pelo Identidade, sob savepoint e locks de pessoa/lista.
- Emitir `linked`, `already_linked` ou `not_found`, com `exception` para falha de contrato/persistência.
- Persistir resultado em `variables.customs[output_var]` e diagnóstico em `source_list_membership_last_result`/`last_error`.
- Não chamar o Target Core, associar mailing ao flow, materializar `contact_list_members`, criar sessões ou oferecer remoção.

### VALIDATION

- Testes focados do novo card, `create_contact` e Identidade, incluindo PostgreSQL real: `54 passed`.
- Teste transacional em PostgreSQL real com tabelas temporárias: `1 passed`; primeira execução `linked`, segunda `already_linked`, exatamente um draft/vínculo/canal e contadores incrementados uma vez. As tabelas foram descartadas no commit e não tocaram dados compartilhados.
- Regressão ampliada de workflow fora da sandbox: `181 passed, 9 failed`; as nove falhas pertencem à baseline conhecida (sete usam a assinatura legada `trigger_orch(flow_uuid=...)` e duas expectativas dependem de estado compartilhado), sem falha nova atribuível ao card.
- `py_compile` e `git diff --check` passaram. `ruff` não está instalado na `.venv` atual.
- Stack local completa reiniciada no perfil isolado `f5_local`; API, três workers e dois Beats permaneceram `up`. Os smokes canônicos criaram as sessões `7347` e `7348`, ambas concluídas em `state=3`.
- Canário E2E no flow `67c00879-f9e3-4ed3-82c0-a695970acc2b`, revisão publicada `c12c83b2-f6ca-4870-b198-8f3f08cc70ac`: a sessão `7349` seguiu por `linked`, criou um vínculo/draft com 8 canais e incrementou `rows_total/rows_processed` de `1/1` para `2/2`; a sessão `7350` seguiu por `already_linked`, reutilizou o mesmo draft e não alterou contadores. Ambas terminaram em `state=3`, sem alarmes.
- Após as duas execuções, havia exatamente duas sessões do flow, um vínculo em `source_list_contact_drafts`, zero `contact_list_members` para pessoa/lista e zero `flow_mailing_links` ativos. Os dois marcadores `api_call` foram confirmados pelo destino com HTTP 200/`received` em uma tentativa.
- No encerramento, `dev_phase_stack.sh stop` reproduziu o risco já documentado de filhos órfãos. Os PIDs foram associados ao worktree/filas `f5_local`, encerrados explicitamente e a auditoria final confirmou porta `7777` livre e nenhum processo das filas locais.

### POST-DEPLOY

- O merge `c0b1c35` da PR ORCH `#147` foi implantado nos hosts `10.1.20.136` e `10.1.20.237`. Os `.env` locais foram copiados para backups temporários com modo `0600`, comparados byte a byte após o fast-forward e preservaram seus checksums; os backups foram removidos ao final.
- No `10.1.20.136`, somente `orch-api.service` foi reiniciada, preservando a topologia sem workers. No `10.1.20.237`, `orch-api.service` e `orch-celery-worker_01..05.service` foram reiniciados em rolling restart; FileApp, generate-file e billing não foram interrompidos.
- Os quatro health checks retornaram HTTP 200 nos dois hosts aplicáveis, os cinco workers de workflow responderam `pong` e nenhuma unit ORCH ficou em estado `failed`.
- O canário pós-deploy `7351`, na revisão publicada v2 `265f8a89-fe8c-44fe-a999-8e673e92fdaf`, terminou em `state=3` pelo ramo `already_linked`, reutilizou o draft de 8 canais, não alterou os contadores `2/2` e não gerou alarme.
- A auditoria confirmou um único vínculo em `source_list_contact_drafts`, zero `contact_list_members`, zero `flow_mailing_links`, três sessões totais do flow e nenhuma ativa. O marcador `api_call` foi observado no destino com HTTP 200/`received` em uma tentativa; a auditoria tardia permaneceu idêntica, sem fan-out.

### RISK / ROLLBACK

O principal risco seria confundir associação à source list com materialização no flow. O card é deliberadamente aditivo e local; remoção e materialização permanecem fora do contrato. Para rollback, interromper novos usos, reverter a PR `#147`/commit funcional `c5128f5` e reiniciar API/workers de workflow; não apagar automaticamente drafts funcionais já criados.

## 2026-09-06 — Serialização de `contact_birth_date` no runtime

### REQUEST / CLASSIFICATION

Corrigir o retry storm descoberto durante o canário de `create_contact.update_current`. `ALPHA_FIX_REQUIRED`; mudança cirúrgica no executor M2, sem migration, fila ou contrato externo novo.

### CAUSE

O contexto SQL retorna `contact_birth_date` como `datetime.date`. `_inject_contact_runtime_scope` armazenava o objeto cru nos aliases `variables.contact` e `variables.customs.contact`; a chamada seguinte a `replace_session_workflow_state` executava `json.dumps` e lançava `TypeError`, revertendo card e cursor. A sessão `7324` repetiu esse caminho 661 vezes.

### CHANGE

- Converter `date`/`datetime` para ISO apenas ao montar `contact.birth_date`.
- Preservar strings e `None` já válidos, todos os demais campos e o comportamento do card.
- Cobrir os dois aliases e a serialização completa do runtime em teste unitário.

### VALIDATION

- Teste específico de injeção: `3 passed`; regressão `create_contact`/workflow: `132 passed`.
- Suíte completa: `488 passed, 28 failed`; 27 falhas pertencem à baseline legada e o `InvalidCachedStatementError` adicional passou isoladamente.
- Stack local completa reiniciada em terminal dedicado, com API, três workers e dois Beats `up`.
- Smokes canônicos: sessões `7337` e `7338`, ambas encerradas em `state=3`.
- Canário E2E `7340`: revisão draft fixada, `birth_date=1940-08-12`, resultado `updated` nos campos `state/city`, zero alarmes, sessão `state=3` e confirmação externa HTTP 200/`received`.
- Restauração auditada: definição, checksum, draft, ponteiro, pessoa e `updated_at` voltaram exatamente à baseline; zero sessão ativa e zero alarme tardio no canário.

### POST-DEPLOY

- O merge `792f39e` foi implantado nos hosts `10.1.20.136` e `10.1.20.237`, preservando byte a byte os `.env` locais durante a atualização.
- A API foi reiniciada nos dois hosts; os cinco workers ORCH foram reiniciados no `10.1.20.237`. Health, nós Celery e unidades ORCH permaneceram saudáveis.
- O canário `7341` terminou em `state=3`, sem alarmes, com `birth_date=1940-08-12`, resultado `updated` nos campos controlados e POST externo confirmado com HTTP 200/`received`.
- A restauração de definição, revisão, checksum, estado draft e pessoa foi confirmada. A auditoria tardia encontrou zero sessões ativas, zero alarmes e nenhuma nova falha de serialização nos logs desde o restart.

### ROLLBACK

Reverter o merge `792f39e` (ou o commit funcional `d8f55f7`) e reiniciar API/workers. Não há migration nem dado novo persistente. Não executar novo `update_current` com contato que possua data de nascimento enquanto o código antigo estiver ativo.

## 2026-09-06 — Compatibilidade de `birthdate` no card `identidade_person`

### REQUEST / CLASSIFICATION

Corrigir a falha do primeiro canário de escrita do card em produção. `ALPHA_FIX_REQUIRED`; impacto restrito à persistência de pessoa/draft do `identidade_person`, sem migration.

### CAUSE

O retorno da Identidade fornece `birthday` como string ISO e o runtime precisa preservar esse formato serializável. A mesma string era enviada sem conversão aos parâmetros das colunas PostgreSQL `date`; o `asyncpg` falhou com `DataError`, SQLSTATE `22000`, ao tentar codificá-la (`toordinal`). O card encapsulou a causa em `identidade_person_persistence_failed` e seguiu corretamente pelo ramo `exception`.

### CHANGE

- Converter `birthdate` para `datetime.date` somente na fronteira do repositório SQL.
- Aplicar a conversão em criação e atualização de `persons` e em criação/atualização de `contact_drafts`.
- Preservar a string ISO no runtime e rejeitar formato inválido sem coerção silenciosa.

### VALIDATION

- O canário que revelou o defeito criou exatamente uma sessão e não deixou pessoa, draft, membro, vínculo ou incremento parcial na lista.
- Probe anterior ao patch reproduziu `DBAPIError`/`DataError`, SQLSTATE `22000`, com `birthdate` Python do tipo `str`.
- Com o patch carregado isoladamente no runtime do host `10.1.20.237`, a transação real criou 1 pessoa, 1 draft e 8 canais; o valor retornado pelo banco foi `date`. O rollback confirmou zero pessoa e zero draft residuais.
- Stack local completa: API, três workers e dois Beats ficaram `up`; o smoke canônico criou as sessões `7246` e `7247`, ambas encerradas em `state=3`.
- Canário E2E pré-deploy `1b54233b-7075-42c9-8085-35c8afad5db7`: pessoa criada; lista passou de 3 para 4; 1 draft e 8 canais/membros materializados; vínculo ativo confirmado pelo Target Core com HTTP 200 em uma tentativa; `api_call` também registrou HTTP 200 em uma tentativa; sessão terminou em `state=3`, sem erro nem cache pendente.
- A contagem de sessões do flow passou de 2 para 3, comprovando que o vínculo protegido não gerou fan-out ou recursão. A observação independente no destino do `api_call` não foi realizada.
- Repetir o canário após deploy antes de considerar a versão implantada validada.

### ROLLBACK

Desabilitar novas execuções do card e reverter o commit do repositório; não há migration. O canário E2E criou intencionalmente pessoa, draft, canais, membros e vínculo no workspace de teste compartilhado. Esses dados não devem ser removidos automaticamente no rollback de código; eventual limpeza precisa ser uma operação explícita e auditada.

## 2026-09-05 — Engine inicial do card `identidade_person`

### REQUEST / CLASSIFICATION

Implementar no ORCH o componente de consulta e enriquecimento criado no catálogo do Target Core. `ALPHA_FIX_OPTIONAL`; risco alto por PII, API potencialmente cobrada e escrita em tabelas compartilhadas.

### CHANGE

- Cliente com URL fixa, Bearer sem logging, timeout, retry transitório e validação de CPF/workspace/resposta.
- Normalização de dados, exclusão de DND dos canais acionáveis e políticas `lookup_only`, `create_if_missing`, `enrich_if_found` e `upsert`.
- Associação idempotente a lista por `contact_drafts`/`source_list_contact_drafts`, dentro de savepoint; resposta externa bem-sucedida fica pendente no runtime somente até concluir, evitando nova cobrança após falha de persistência.
- Branches `encontrado`, `nao_encontrado` e `exception`; formatos mistos emitidos pela UI foram cobertos.
- O vínculo mailing→flow bloqueia a sessão e roda em task após o commit local. A origem `identidade_person` é validada pelo Target Core, atualiza vínculos ativos idempotentemente e usa `skip_orch_sessions=True`.

### VALIDATION

- Regressão consolidada ORCH: 159 testes passaram (155 unitários e 4 integrados contra o PostgreSQL, estes executados fora da sandbox). No Target Core, 14 testes do novo contrato passaram.
- PostgreSQL real: pessoa, draft e canal foram criados dentro de transação de teste e desapareceram após rollback (`persons_count=0`, `drafts_count=0`).
- Um teste preexistente de concorrência permanece vermelho no `origin/main` por chamar `trigger_orch(flow_uuid=...)`, assinatura que não existe mais; não foi alterado por este patch.
- Stack local completa subiu no perfil isolado `f5_local`; API e cinco processos Celery ficaram `up`. O smoke canônico aceitou uma sessão em cada fluxo de regressão e ambas concluíram em `state=3` (`7233` e `7234`).
- Canário real autorizado no flow `4e7340ee-ac17-488d-953f-46c51d7b2cd3`: sessão `2dd62260-3519-45dd-9275-ad0c56359b84` consultou a Identidade.io uma vez, terminou em `state=3`, seguiu `lookup_only` e deixou contagens de pessoa/draft/canal inalteradas em zero.
- O E2E de escrita mais vínculo permanece pendente até os dois patches serem implantados em ordem Target Core → ORCH; nenhuma escrita de produção foi feita nesta etapa.

### ROLLBACK

Desabilitar/remover o card dos flows antes de reverter. Reverter ORCH e Target Core não exige migration; a ordem de rollback segura é interromper novas execuções, reverter ORCH e depois Target Core.

## 2026-09-04 — Contrato ORCH para sessões por pessoa e canal exato

### REQUEST / CLASSIFICATION

Suportar o piloto em que o Target Core reduz o fanout para uma sessão por pessoa sem devolver ao Target a decisão de `linked_actuator`. `ALPHA_FIX_REQUIRED`; risco alto por identidade de membro, canal e comunicação externa.

### CHANGE

- Escopo explícito valida o endereço de `orch_sessions` contra `contact_list_members`, além de membro/lista/mailing; o tipo enviado também é validado (`phone` e `voice` são equivalentes).
- `session_scope=person` força roteamento contextual mesmo quando a flag histórica está desligada; ausência ou valor desconhecido preserva `channel`.
- O ORCH não define atuador na criação. Sessão `person` que alcança card de saída Dialer/WhatsApp termina com erro e alarme próprios, antes do efeito externo.
- O fixture PostgreSQL de roteamento contextual foi alinhado às colunas HSM já usadas pelo runtime.

### VALIDATION

- Regressão local de repositório, M2 e rota manual: 130 testes passaram.
- PostgreSQL real com tabelas temporárias: 1 teste passou, incluindo rejeição por endereço conflitante. Uma execução anterior invalidou o prepared statement do `asyncpg` após mudança do schema temporário; a repetição com schema estável passou.
- E2E cruzado ainda está pendente. Nenhum deploy ou escrita de produção foi executado.

## 2026-08-28 — Billing batch persistente `service-orch`

### REQUEST / CLASSIFICATION

Substituir o publisher unitario legado por event store + snapshots agregados, retry persistente, reconciliacao e reprocessamento auditavel. `ALPHA_FIX_OPTIONAL`, risco alto por banco, Celery, concorrencia, idempotencia e RabbitMQ; novo mecanismo protegido por flag default `false`.

### CHANGE

- Legado `ORCH_BILLING_SNAPSHOT_ENABLED` preservado e desligado por default; configuracao rejeita dual enablement com `ORCH_BILLING_ENABLED`.
- Migration `0022` cria eventos, snapshots, solicitacoes de reprocessamento e indice temporal de `orch_sessions`, sem alterar/remover a tabela `0020`.
- Sessao nova, inclusive pelo componente `create_contact`, registra evento fail-open; reconciliador usa `orch_sessions` e chave idempotente por workspace/sessao/periodo/metrica.
- Agregador cria batches maximos de 200 sob `FOR UPDATE SKIP LOCKED` e chama publicacao no mesmo flush; publisher usa payload persistido, `mandatory`, mensagem persistente e confirm.
- Retry indefinido, backoff/teto/jitter, leases com `claim_token`, bloqueio estrutural e reprocessamento idempotente foram adicionados.
- Reprocessamento mensal usa chunks persistentes de 1000, cursor retomavel e reserva de enqueue para nao inflar a fila quando workers estiverem indisponiveis.
- App/fila/worker/Beat dedicados e rotas autenticadas de reprocess/status foram adicionados; nenhum template foi instalado.

### VALIDATION

- Regressao focada final: 157 testes passaram.
- Integracao PostgreSQL fora da sandbox: 2 testes passaram; 450 sessoes em tabelas temporarias viraram 450 eventos e snapshots `200 + 200 + 50`, e a migration exata executou em schema descartavel dentro de transacao revertida.
- Suite completa: 415 passaram e 27 falharam, exatamente nas duas familias do baseline (26 chamadas com assinatura antiga de `trigger_orch` e 1 invalidacao de prepared statement asyncpg em tabela temporaria); nenhuma falha nova de billing.
- `compileall` e `git diff --check` passaram. Revisao independente concluiu `GO` para o patch de codigo.
- Stack local homologada: API, tres workers e dois Beats `up`; smoke real nos dois flows retornou `202 accepted` para as sessoes `7201` e `7202`. Billing permaneceu desligado e nenhuma migration foi aplicada.
- Broker/consumer alvo, concorrencia real multi-transacao, lock do indice em workspace volumoso e E2E do billing apos migration permanecem `UNKNOWN`; nenhuma producao foi acessada.

### ROLLBACK

`ORCH_BILLING_ENABLED=false`, restart dos processos afetados e preservacao das tabelas. Nao reativar o publisher legado sem decisao operacional explicita.

## 2026-08-27 — Card isolado `switch_bot_flow`

### REQUEST

Criar um card separado de `run_flow` que transforme a sessao ORCH em hub WhatsApp, repassando ao BOT o payload Meta original desde o primeiro evento ate o callback terminal.

### TASK TYPE / CLASSIFICATION

Feature operacional isolada / `ALPHA_FIX_OPTIONAL`; blast radius contido por card, feature flag e fila dedicada. `run_flow` nao foi alterado.

### CHANGE

- M2 reconhece `switch_bot_flow`, persiste estado bloqueante e resolve `success`/`exception_*` somente no terminal.
- Worker dedicado consulta e cacheia `runner_token`, envia somente mensagens de usuario ao Runner v5 e descarta status WhatsApp do relay.
- O body do primeiro e dos eventos seguintes preserva o mesmo conteudo JSON recebido da Meta, sem envelope sintetico.
- `target_session_id` e metadados da revisao ficam persistidos; callback idempotente retoma o M2 e o primeiro terminal vence.
- Filas isoladas foram adicionadas aos profiles DEV/launchd/prod e aos manifests versionados.

### VALIDATION

- Regressao direcionada final: 135 testes passaram; `compileall`, `git diff --check`, `bash -n` e `plutil -lint` passaram.
- Suite completa: 354 passaram e 26 falharam em baseline legado, primeiro por `trigger_orch(flow_uuid=...)` desatualizado.
- Consulta real ao Target Core resolveu um `runner_token` de 64 caracteres sem expor o segredo.
- Stack local completa subiu com `orch_switch_bot_flow_f5_local`; worker registrou a task e o smoke encadeado dos dois flows retornou aceite.
- POST real ao Runner, resposta do BOT via Meta e callback terminal permanecem pendentes de canario controlado para evitar mensagem externa acidental.

### CANARIO DE PRODUCAO — 2026-08-27

- O template anterior ao card chegou ao contato, confirmando ausencia de regressao no caminho existente.
- O POST real ao Runner foi aceito e a engine gerou resposta, mas cada payload criou uma sessao nova porque o token usava `session_key=chat.id` e a entrada foi classificada como provider `webhook`.
- Todos os dispatches do BOT terminaram em `missing_integration`; nenhuma resposta chegou a Meta.
- O ORCH detectou a troca de `session_id`, persistiu `switch_bot_flow_runner_session_mismatch` e percorreu o branch de excecao como projetado.
- Auditoria read-only posterior confirmou que o provider e definido pela URL. O ORCH usou `/webhook/session`, enquanto `/whatsapp/session` ja interpreta o envelope Meta, mantem a identidade pelo `wa_id` e despacha pela API WhatsApp generica configurada nos hosts Target Core; nao foi identificada necessidade de alterar o codigo do Target Core antes do proximo canario.
- Diagnostico completo em `INCIDENT_HISTORY.md` e risco `R28` em `KNOWN_RISKS.md`. Nenhum ajuste funcional ou de producao foi feito durante a investigacao.
- Correcao preparada no ORCH: troca cirurgica do provider da URL para `whatsapp`, sem alterar payload, retry, correlacao local ou contratos dos demais cards. Deploy e novo canario permanecem pendentes.
- O segundo canario confirmou o ciclo completo de mensagens e o contrato terminal real do BOT: `POST /v1/orch/{alias}` com `entity`, `session.id`, `variables` e `disposition`. O `session.id` `9706f438-80be-47b7-a0e4-9923b1c489f0` coincidiu exatamente com o `target_session_id` da sessao ORCH `7105`.
- Antes da correcao, esse terminal entrou como `GenericApp` e criou a sessao fantasma `7106`, enquanto `7105` permaneceu bloqueada. A correcao Alpha intercepta apenas o caminho por alias, exige correlacao exata com um handoff do mesmo flow, reaproveita o callback idempotente e preserva o trigger legado quando nao houver correspondencia.
- Regressao direcionada final da correcao terminal: 130 testes passaram; lint/compile e `git diff --check` passaram. A stack local completa ficou `up`, o smoke encadeado dos dois flows passou e o replay HTTP do payload real retornou `persistence=switch_bot_flow_callback`, `session_created=false` e `session_id=7105`. O worker levou a sessao `7105` a `state=3` com `ended_at`; o replay seguinte retornou `idempotent=true` e `session_state=3`, sem criar nova sessao. A stack local foi encerrada sem processos residuais.

### ROLLBACK

Definir `SWITCH_BOT_FLOW_ENABLED=false` e reiniciar API/worker. Flows sem o novo card e o comportamento de `run_flow` permanecem inalterados.

## 2026-08-26 — Recibo idempotente para entrega imediata FileApp

### REQUEST

Eliminar a dependência operacional do rescue de 10 minutos para eventos S3/FileApp do fluxo crítico, mantendo o rescue como rede de segurança.

### TASK TYPE / CLASSIFICATION

Incident remediation / `ALPHA_FIX_REQUIRED` (high risk: FileApp, Celery e migration multi-workspace).

### CHANGE

- A migration `0021_create_fileapp_ingest_receipts` cria um recibo por `(flow_uuid, file_id)` no schema do workspace.
- O trigger Tipo 1 reivindica e confirma o recibo antes de publicar no Celery; replays em estados ativos/terminais retornam `202` idempotente sem novo enqueue.
- O worker Tipo 1 carrega o `receipt_id` e registra `processing`, `completed` ou `failed` em modo best-effort.
- O rescue reivindica o mesmo recibo antes de publicar; um recibo originado pelo webhook impede reingestão duplicada.
- O recibo agora expõe explicitamente `should_enqueue`: uma primeira recepção e a retomada de `failed`/`enqueue_failed` publicam uma task; estados ativos ou concluídos permanecem replays idempotentes.
- A correção residual remove o receipt de entrega Target Core da decisão de “ingestão existente”, recupera `accepted` stale após 60 segundos, registra `task_id` no enqueue do rescue e usa o batch como limite de ações, sem starvation por skips.
- A correção da corrida do step 5 aceita `INGESTING`/`PROCESSED` como auto-ingestão já iniciada/concluída pelo Target Core e não publica um segundo import nesses estados; estados desconhecidos continuam fail-closed.

### VALIDATION

- `pytest -q tests/test_fileapp_ingest_tasks.py tests/test_fileapp_entrada_rescue_task.py tests/test_migration_service.py`: 22 passed.
- Após a correção de retomada: `pytest -q tests/test_fileapp_ingest_receipt_api.py tests/test_fileapp_entrada_rescue_task.py tests/test_fileapp_ingest_tasks.py`: 22 passed; `compileall` dos módulos alterados passou.
- Runtime de produção após o merge `613ac46`: API `ready=true`; cinco workers FileApp ativos; 15 arquivos reenviados pela rota oficial retornaram `202 queued`, concluíram com receipts `completed` e foram encontrados em `monitoramento/upload/processados`; zero em `falha`, zero ausentes e zero restantes em `monitoramento/upload`.
- O runtime também confirmou risco residual: `arquivos_s3_events` pode fazer o rescue marcar `done` sem `source_list`, mantendo o arquivo físico e causando starvation com batch `2`; registrado em `KNOWN_RISKS.md` R25.
- Correção residual: 28 testes passaram; `compileall` passou; claim validado no PostgreSQL real com primeira aceitação, replay fresco bloqueado e reclaim stale permitido, tudo revertido por rollback.
- Stack local completa subiu com filas `f5_local`, API/workers ficaram prontos e o smoke canônico dos dois flows passou. A stack foi encerrada e não restaram Uvicorn/Celery locais do repositório.
- Validação de migration em DB real, subida da stack e E2E cruzado com Target Core permanecem pendentes.
- Testes focados da corrida do step 5: `12 passed`; regressão FileApp ampliada: `73 passed`, cobrindo `READY_TO_INGEST`, `INGESTING`, `PROCESSED` e rejeição de estado regressivo; `compileall` passou. A stack local completa ficou pronta e o smoke canônico dos dois flows passou; nenhum processo local permaneceu ativo depois da validação.

### ROLLBACK

Desabilitar a migration/código desta entrega antes do rollout; após a migration ser aplicada, preservar a tabela e reverter apenas os consumidores para manter os recibos auditáveis.

## 2026-08-24 — Primeiro onboarding formal do Project Steward

### REQUEST

Executar integralmente a secao `PRIMEIRA EXECUCAO`, sem alterar codigo funcional, usando arqueologia paralela, revisao adversarial e memoria persistente.

### TASK TYPE

Investigation / documentation.

### CHANGE

- Criado `PROJECT_BRAIN.md`.
- Criada baseline em `docs/project-knowledge/` para arquitetura, componentes, fluxos, dados, dependencias, integracoes, configuracao, operacao, quirks, riscos e divida tecnica.
- Nenhum arquivo funcional, migration, fila, dependencia ou configuracao de producao foi alterado.

### EVIDENCE

- `AGENTS.md`, `PROJECT_STEWARD.md` e `README.md` lidos integralmente.
- Runbooks, scripts, units, SQL, rotas, services, repositories, tasks, testes e historico Git inspecionados.
- Cinco arqueologias independentes: API/workflow, Celery, dados/migrations, FileApp e runtime/integracoes.
- Tres revisoes adversariais concluidas sobre os achados de maior risco; nenhuma das conclusoes centrais foi refutada, e qualificacoes foram incorporadas.

### VALIDATION

- `pytest --collect-only -q`: 295 casos.
- `pytest -q` fora da sandbox: 270 passaram, 25 falharam em 192.08s.
- As 25 falhas observadas param primeiro em `trigger_orch(flow_uuid=...)`, assinatura anterior a `alias_or_flow_uuid`; revisao estatica indica que alguns casos podem ter expectativas semanticas adicionais desatualizadas.
- Runtime local completo nao foi iniciado; health/E2E permanecem `UNKNOWN`.

### RISK

Documentacao: low. Achados operacionais registrados em `KNOWN_RISKS.md`, sem correcao nesta etapa.

### ROLLBACK

Remover somente `PROJECT_BRAIN.md` e `docs/project-knowledge/` criados neste onboarding.

### NOTES

A baseline e majoritariamente estatica. Estado de producao, integracoes e persistencia FileApp tipo 1 precisam de verificacao observacional/E2E futura.

## 2026-08-24 — Analise do flow ORQUESTRADOR

### REQUEST

Determinar se o flow `0e378237-4a61-4d5f-89f3-b07b594df38f` possui gaps.

### TASK TYPE

Incident / production analysis, somente leitura.

### EVIDENCE

- Flow localizado no workspace `91c85c54-cd88-4aed-88e0-7eb720674f5d`, ativo em cadastro, mas `draft` e sem revisao publicada.
- Grafo e revisao draft extraidos do PostgreSQL; implementacao M1/M2, dispatcher, executor e FileApp confrontada com a definicao.
- Tres sessoes encontradas no cursor inicial, quatro eventos WhatsApp pendentes e 1.136.779 alarmes de execucao ate 2026-08-24 11:35 BRT.
- Quatro workers workflow observados consumindo as filas `orch_dispatch`, `orch_execute` e `orch_heartbeat` em dois hosts.
- Revisao adversarial independente confirmou os gaps centrais e qualificou como hipotese a atribuicao de versoes distintas aos workers.

### FINDINGS

- Condition sem branch `false/exception`, embora declare `has_exception_branch=true`.
- `api_call` de EMAIL e SMS sem URL; EMAIL sem branches `error/exception`.
- Flow filho de pre-vendas tambem esta somente em draft.
- Mapping template existente, mas `QuantidadeParcelas` e `Cpf` apontam simultaneamente para `CONTACT_IDENTIFIER`.
- Erros permanentes sao amplificados por reenfileiramento continuo; nenhuma intervencao foi executada.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED`, risco critical. Detalhes em `docs/project-knowledge/INCIDENT_HISTORY.md`.

## 2026-08-24 — Analise do flow Demo WhatsApp Outbound

### REQUEST

Avaliar o flow `4d81d73b-dfee-43b8-9c82-d3c52207941f` no workspace `ba7eb0ec-e565-447c-8c11-8f870cf72a60`, com atencao a uma sessao relacionada ao telefone informado pelo operador.

### TASK TYPE

Incident / production analysis, somente leitura.

### EVIDENCE

- Revisao publicada v13 extraida e grafo confrontado com runtime, metricas, sessoes e eventos.
- A sessao `6927` percorreu `confirmar -> set_variables -> api_call 200 -> live` e terminou em `component_not_supported:live`; nao havia sessao nao terminal para o telefone consultado na fotografia de 15:27 BRT.
- Sete sessoes GenericApp `state=0` acumulavam 4.389.386 execucoes `blocked_send_whatsapp_interactive` sem alarmes ate 15:28 BRT.
- Branch atual, `main` e commit isolado `bd461a5` comparados sem checkout ou alteracao de Git.
- Revisao adversarial independente identificou claim sem commit e omissao de `blocked_send_whatsapp_interactive` na transicao defensiva como defeitos distintos.

### FINDINGS

- Retry storm silencioso confirmado e ainda ativo quando observado.
- Handoff humano nao ocorreu para a resposta do operador.
- O suporte `live` isolado no historico nao constitui correcao pronta, pois nao executa side effect externo.
- Semantica de `encerrar`, aliases de eventos e convergencia dos outcomes da API precisam de confirmacao funcional.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED`, risco critical. Detalhes em `docs/project-knowledge/INCIDENT_HISTORY.md` e `docs/project-knowledge/KNOWN_RISKS.md`.

## 2026-08-24 — Contencao e correcao do retry storm por branch ausente

### REQUEST

Terminalizar as sessoes presas do flow `0e378237-4a61-4d5f-89f3-b07b594df38f` e fazer `condition` sem branch compativel encerrar a sessao como falha.

### TASK TYPE

Production containment / `ALPHA_FIX_REQUIRED`.

### CONTAINMENT

- Fotografia imediatamente anterior: sessoes `256` e `257` em `state=0`, cursor inicial e `ended_at=NULL`; 1.153.946 alarmes, ainda crescendo.
- Update transacional e guardado terminalizou somente esses IDs com marcador de falha operacional.
- Houve drenagem residual de 28 alarmes de tasks anteriores; a primeira contagem estabilizou em 1.153.974.
- A stack local `f5_local` usada na validacao permaneceu ativa e revelou que `CELERY_DISPATCH_WORKSPACE_UUID` nao escopa o reconciliador. Sem `CELERY_RECONCILE_PENDING_EVENTS_WORKSPACE_UUID`, ele encontrou a sessao WhatsApp `263`, `state=2`, em outro workspace e gerou 51 alarmes adicionais por `api_call_missing_url`.
- A sessao `263` foi terminalizada com guard exato as 16:50 BRT e a stack local foi parada pelo script oficial. O ultimo alarme antecedeu a terminalizacao; a contagem final permaneceu em 1.154.025 apos nova janela de 60 segundos.
- Nenhuma sessao elegivel permaneceu no flow.

### CHANGE

- `condition_branch_not_mapped` agora terminaliza com `state=3`, `ended_at`, cursor nulo e runtime de diagnostico.
- Async Celery registra alarme/metricas como erro e commita a terminalizacao.
- Tasks atrasadas param somente quando encontram estado terminal acompanhado de `workflow_v2.terminal_failure`.
- Fluxos normais que chegam a M2 em `state=3`, como callbacks finais de canal, permanecem inalterados.

### VALIDATION

- `git diff --check`: passou.
- `compileall` dos arquivos alterados: passou.
- Testes focados sem DB: 87 passaram.
- Primeira revisao adversarial encontrou guard terminal amplo demais; correcao aplicada.
- Segunda revisao adversarial: nenhum achado acionavel.
- Smoke isolado, sem beat/dispatcher: API local retornou `202` para os fluxos canonicos A e B; as duas tasks foram recebidas e concluidas com sucesso pelo worker dedicado.
- A tentativa anterior de stack completa nao conta como validacao segura de isolamento: o dispatcher estava escopado, mas o reconciliador nao. O incidente e o risco foram documentados.
- Teste DB nao foi repetido porque o `.env` aponta para workspace real; a suite DB existente continua com casos stale que usam `trigger_orch(flow_uuid=...)`.

### ROLLBACK

- Codigo: reverter os arquivos funcionais desta manutencao.
- Sessoes contidas: restauracao exige decisao operacional explicita; nao reabrir enquanto o flow permanecer invalido.

### DEPLOYMENT

Nao executado nesta etapa. A contenção de producao esta ativa; a correcao protege novas sessoes somente apos deploy/restart validado.

## 2026-08-24 — Investigacao de `linked_actuator` ausente no flow de Dialer

### REQUEST

Investigar por que o membro da lista `dc7dc1c1-2c98-42e9-a788-5d186f458daa` permanecia sem `linked_actuator=dialer` no flow `3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17`.

### TASK TYPE

Production diagnosis read-only / `ALPHA_FIX_REQUIRED`.

### EVIDENCE

- Branch `investigate/dialer-linked-actuator` criado de `origin/main` no merge `1dc8494`.
- Flow ativo, revisao publicada v5 e primeiro card `send_with_dialer` confirmados.
- Sessao ativa `6937` executou o card e persistiu `blocked_send_with_dialer`.
- Payload de `6928/6937`: lista `dc7dc1c1-2c98-42e9-a788-5d186f458daa`, mailing `1115`.
- Runtime de ambas: assignment para membro `10687`, lista `b5521cb2-09a9-4391-8ab5-fea25924e820`, mailing `1114`.
- O membro esperado `10655` permaneceu nulo; o membro `10687` recebeu `dialer`.
- Blast radius conservador: 38 identificadores duplicados ativos, 113 linhas e 26 sessoes divergentes em tres flows.

### FINDING

O ORCH nao deixou de setar o actuator. Ele o setou na linha errada porque o seletor ignora a lista/mailing do payload e escolhe o membro ativo mais novo para o mesmo `contact_identifier`. O mesmo padrao existe no roteamento WhatsApp e no carregamento do contexto de contato.

### REVIEW

Revisao adversarial independente confirmou a causa e recomendou resolver o membro por identidade contextual, com fallback legado apenas na ausencia completa de seletores.

### CHANGE

Na etapa inicial, nenhum codigo funcional ou dado de producao foi alterado. Apos aprovacao, foi implementada correcao protegida por feature flag default-off:

- escopo imutavel extraido de `input_payload`;
- resolucao unica e reutilizada por contexto, Dialer e WhatsApp;
- seletores combinados validados cruzadamente, sem fallback em conflito;
- queries especializadas com tipos nativos `uuid`/`bigint`;
- lock/revalidacao antes do update e falha terminal em corrida;
- alarmes equivalentes nos caminhos Celery e inline.

### VALIDATION

- tipos reais confirmados read-only no workspace: `contact_list_id=uuid`, `mailing_id=bigint`;
- `python -m py_compile`: passou nos arquivos alterados;
- `git diff --check`: passou;
- testes focados de M2, repositorio e tasks: 105 passaram;
- duas revisoes adversariais independentes executadas; os achados bloqueantes foram incorporados;
- runtime isolado e smoke E2E ainda pendentes.

Validacao posterior:

- consulta read-only no caso real confirmou: fallback legado -> membro `10687`; escopo exato/lista+mailing -> `10655`; conflito cruzado -> nenhum membro;
- teste PostgreSQL com tabelas temporarias executou resolucao, `FOR UPDATE` e updates Dialer/WhatsApp sem tocar tabelas permanentes; 1 passou;
- a primeira execucao desse teste encontrou parametro asyncpg ambiguo nos atuadores; a query foi especializada e a repeticao passou;
- tentativa de stack completa invalidada por processos stale nao controlados pelo script; todos os processos locais foram contidos, nenhum consumer `f5_local` permaneceu no broker e R21 foi registrado;
- nenhum smoke HTTP foi enviado. O E2E de stack permanece pendente.
- formatos reais agregados no workspace: 59 payloads com `contact_list_member_id` numerico, 437 com `contact_list_id` UUID e 378 com `mailing_id` numerico; nenhum UUID foi observado nos dois campos `BIGINT`;
- o runbook Supplier exemplificava incorretamente member/mailing como UUID e foi corrigido para refletir o schema e o runtime reais.
- revisao adversarial final do bundle funcional: `GO`, sem achados criticos, altos ou bloqueantes; risco residual limitado a concorrencia real entre transacoes nao simulada;
- follow-up de seguranca: rotacionar a credencial SFTP exibida no terminal por uma consulta de auditoria excessivamente ampla; nenhum segredo foi copiado para arquivos versionados.

### ROLLOUT / ROLLBACK

Implantar inicialmente com `WORKFLOW_CONTEXTUAL_MEMBER_ROUTING_ENABLED=false`; validar com filas e workspace isolados usando `true`; somente depois habilitar no ambiente alvo e reiniciar. Rollback operacional: flag `false` + restart. Reparacao historica permanece separada e nao automatizada.

## 2026-08-24 — Pente-fino pos-migracao dos workers para `10.1.20.237`

### REQUEST

Auditar o runtime depois de varias tasks processadas no novo host e identificar GAPs diretamente ligados ao cutover.

### TASK TYPE

Production audit read-only / `ALPHA_FIX_REQUIRED` para os achados criticos e altos.

### CONFIRMED

- API, tres beats e quinze workers ORCH ativos/enabled no `10.1.20.237`, sem restart de unit ou unit falha; API do `10.1.20.136` permaneceu ativa por restricao do proxy e seus workers/beats ORCH permaneceram desabilitados.
- As oito filas ORCH consultadas passivamente tinham cinco consumers e zero mensagens prontas; `active/reserved/scheduled` nao mostrou backlog de workflow.
- FileApp e generate-file concluiram todas as tasks observadas na primeira janela, sem task failure marker.
- A correcao contextual esta efetiva: 46 sessoes novas explicitas produziram 46 assignments e zero divergencias de membro, lista, mailing ou atuador. A sessao `6941` do flow `3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17` resolveu `10655` e deixou `linked_actuator=dialer`.
- Tres beats publicavam schedules sobrepostos; os arquivos de schedule eram distintos, refutando disputa do arquivo local.
- O loop `blocked_send_whatsapp_interactive` permanecia ativo em tres workspaces. A janela desde 19:22 BRT produziu mais de 213 mil execucoes e 428 mil metricas; o ORCH escreveu aproximadamente 1,27 milhao de linhas/234 MB no journal.
- Os doze schemas com metricas somavam estimativa de 199,4 milhoes de linhas e mais de 90 GB.
- O host tinha 152 processos Celery, cerca de 12,9 GB RSS e 187% de CPU agregada no snapshot; havia capacidade de RAM/disco, mas churn elevado de processos/logs.
- SIGTERM de child process expos `UnboundLocalError` por `stopped_reason` nao inicializado no wrapper da task.
- `/health/celery` aceita qualquer worker do vhost e `/health/ready` nao inclui Celery; ambos sao insuficientes isoladamente para validar o cutover.

### ADVERSARIAL REVIEW

Dois revisores independentes confirmaram: severidade critica para o loop WhatsApp; alta para o wrapper de task, falso positivo de health e readiness sem Celery; critica para publishers duplicados segundo o revisor operacional. A classificacao operacional final manteve a duplicacao como `high`, pois locks/cooldowns limitam parte dos efeitos e nao houve backlog no snapshot.

### UNKNOWN

- Motivo exato dos SIGTERM nos childs.
- Efeito funcional atual de Target Core, Files API, LLM e SFTP fora das tasks observadas.
- Retencao efetiva do journal sob o volume atual e capacidade livre do servidor PostgreSQL.

### CHANGE

Somente documentacao de conhecimento. Nenhum codigo funcional, unit, processo, fila, sessao ou dado de producao foi alterado.

## 2026-08-24 — Correcao do loop `blocked_send_whatsapp_interactive`

### REQUEST

Interromper a reexecucao massiva de sessoes WhatsApp bloqueadas sem alterar o contrato de espera por callback.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED` — risco critico de amplificacao operacional e crescimento de metricas/logs.

### ROOT CAUSE

O executor M2 retorna `blocked_send_whatsapp_interactive` como bloqueio valido para template e interativo, mas o dispatcher nao incluia esse motivo em `BLOCKING_RUNNING_STOP_REASONS`. A sessao podia voltar a `state=0` e ser reclamada novamente pelo scan periodico; cada ciclo criava uma nova task Celery com `Retries=0`, mascarando a tempestade como varias execucoes bem-sucedidas independentes.

### CHANGE

- Inclusao cirurgica de `blocked_send_whatsapp_interactive` no conjunto bloqueante do dispatcher.
- Regressao unitaria exige `state=1`, `only_if_not_finished=True` e ausencia de finalizacao da sessao.
- Nenhuma sessao, fila, unit ou dado de producao foi alterado nesta etapa.

### VALIDATION

- Teste novo falhou antes da mudanca funcional, comprovando a regressao.
- Suite focada: 103 testes passaram em `tests/test_workflow_dispatcher_service.py`, `tests/test_workflow_m2_whatsapp_interactive.py`, `tests/test_workflow_m2_service.py` e `tests/test_workflow_tasks.py`.
- Revisao adversarial independente: `GO`, sem achados bloqueantes; confirmou exclusao dos scans por `state=1` e retomada direta por callback/reconciliador.
- Limitacao conhecida: falta teste integrado PostgreSQL do ciclo completo `bloqueio -> callback/evento -> retomada`.
- Deploy e validacao no host `10.1.20.237` permanecem pendentes.

## 2026-08-24 — Webhook terminal do `finish_flow`

### REQUEST

Suportar os novos campos do card `finish_flow` da revisao publicada do flow `3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17`, enviando os dados da sessao ao webhook e incluindo o evento de telefonia quando o fluxo usa Dialer.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED` — o campo ja publicado nao possuia efeito no runtime.

### CONFIRMED CONTRACT

- Existe apenas um evento de telefonia por sessao; `cdr` e um objeto, nunca lista ou acumulador.
- O payload Dialer e copiado para a sessao dona somente quando a revisao selecionada, consultada no recebimento do evento, possui `finish_flow.parameters.webhook` nao vazio.
- O `finish_flow` envia uma vez a linha completa da sessao com `result` e `cdr`; o CDR nao e duplicado em `runtime_variables`. Resposta `2xx` remove o CDR, enquanto falha o preserva.

### CHANGE

- Persistencia JSONB cirurgica no `runtime_variables.cdr`, sem migration.
- Envio direto pelo mecanismo HTTP ja existente, sem outbox, scanner, beat, fila ou worker adicional.
- Registro do resultado em `runtime_variables.finish_flow_webhook` para diagnostico.

### VALIDATION

- Regressao ampliada: 129 testes passaram; `compileall` e `git diff --check` passaram.
- Teste de execucao completa confirma que o branch `finish_flow` persiste e rele o snapshot terminal antes do dispatch, e que `2xx` remove o unico CDR na persistencia seguinte.
- PostgreSQL real validado com tabela temporaria e rollback: JSONB permanece objeto e uma nova escrita substitui, sem acumular; o snapshot completo e retornado por `to_jsonb`.
- Smoke HTTP real em destino loopback observou um unico `POST`, um unico campo `cdr` e limpeza em memoria apos `204`.
- As revisoes adversariais encontraram dependencia temporal da flag, snapshot parcial, CDR duplicado e leitura anterior a terminalizacao; os quatro achados foram removidos antes do fechamento.
- A revisao final confirmou esses quatro pontos e levantou somente o risco preexistente `R7`: publicacao entre recebimento e execucao pode trocar a revisao relida pelo M2. Pinagem global de revisao foi mantida fora do escopo para nao ampliar o blast radius desta mudanca.
- O POST em destino real foi observado em 2026-08-25; a entrega funcionou e revelou o payload inflado, o reenvio e o ledger pendente registrados na manutencao seguinte.

## 2026-08-25 — Higienizacao e unicidade do webhook `finish_flow`

### REQUEST

Remover repeticoes do payload observado em producao e garantir a paridade entre uma sessao de voz, seu unico CDR e um unico webhook terminal confirmado.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED` — o primeiro teste real enviou estado interno volumoso, repetiu o POST para a mesma sessao e deixou eventos Dialer pendentes sendo reconciliados continuamente.

### RUNTIME EVIDENCE

- A sessao `6945`, workspace `ba7eb0ec-e565-447c-8c11-8f870cf72a60`, flow `3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17`, entregou o webhook com `runtime_variables` inteiro e varias copias do mesmo callback.
- Dois eventos Dialer distintos (`GW02-1787649908.293874` e `GW01-1787649921.293865`) chegaram para a mesma sessao, contrariando o contrato funcional de uma chamada/desfecho por sessao.
- O runtime anterior enviou dois webhooks `200` e deixou os dois registros do ledger com `processed_at=NULL`; o reconciliador continuou executando a sessao terminal.

### CHANGE

- O body passa a conter somente campos persistidos da sessao fora de `runtime_variables`, mais `result` e o unico `cdr` persistido.
- O primeiro resultado `2xx` persistido impede novos POSTs da mesma sessao e remove o CDR.
- O sucesso baixa todos os eventos Dialer excedentes ainda pendentes. Evento tardio apos sucesso e preservado no ledger, mas marcado processado e impedido de recriar o CDR.
- Nenhuma migration, fila, beat, worker ou armazenamento novo foi criado.

### VALIDATION

- Suite focada e ampliada: 132 testes passaram, incluindo payload sem runtime interno, paridade do CDR persistido, supressao de reenvio, limpeza de backlog e tratamento de evento tardio.
- `compileall` e `git diff --check` passaram.
- Smoke HTTP loopback observou exatamente um POST, sem `runtime_variables`, com um CDR; a segunda execucao foi suprimida.
- PostgreSQL real com tabelas temporarias e rollback confirmou CDR unico, bloqueio de ressurgimento apos sucesso e baixa de dois eventos Dialer sem afetar evento WhatsApp.
- A stack local iniciou API, tres workers e dois beats; API e tasks responderam. O runbook abortou e limpou os processos porque `scripts/dev_phase_stack.sh` passa simultaneamente `--hostname` e `-n`: o nome efetivo nao casa com o regex de readiness. O gap e preexistente e ficou fora do patch funcional.
- Duas revisoes adversariais inicialmente deram `NO-GO`; a implementacao foi simplificada para remover fallback de CDR divergente e claim antecipado no `send_with_dialer`. O risco residual best-effort entre `2xx` e commit permanece documentado em R24.
- Deploy e validacao E2E no host `10.1.20.237` permanecem pendentes.

## 2026-08-25 — Paridade deterministica entre sessao, contato e CDR

### REQUEST

Corrigir o teste de producao em que o primeiro webhook higienizado saiu sem CDR e a execucao seguinte nao produziu POST, preservando o contrato de uma sessao de voz, um desfecho e um CDR cru.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED` — perda de dado terminal e supressao silenciosa de webhook em producao.

### ROOT CAUSE

- O executor carregava um snapshot terminal, mas `_dispatch_finish_flow_webhook` buscava `cdr` em outro dicionario de runtime potencialmente antigo.
- `replace_session_workflow_state` substituia o JSONB completo antes do dispatch, ampliando a janela de perda da escrita cirurgica feita no ingresso.
- O sucesso era por sessao e o codigo baixava todos os eventos Dialer pendentes; o fallback temporal ainda podia reabrir uma sessao ja confirmada.
- O payload achatava a sessao e removia o unico lugar de onde o contato normalizado era obtido.

### CHANGE

- `orch_channel_events` passa a ser a fonte autoritativa do CDR cru no `finish_flow`; a copia residual em runtime nunca e usada como fallback de envio.
- O body e `session` (dados persistidos, `result`, `contact`) mais `cdr`; estado interno nao sai.
- Fluxo Dialer sem CDR adia o POST; `2xx` processa somente o evento selecionado e limpa a copia transitoria.
- Evento tardio e marcado individualmente e o fallback recente exclui sessao com webhook ja confirmado.
- Sem migration ou infraestrutura nova.

### VALIDATION

- Suite focada: 31 testes passaram.
- Regressao relevante de trigger, Dialer, workflow e repositorios: 153 testes passaram.
- Teste adversarial reproduz runtime local sem CDR e confirma que o payload cru vem do ledger, com o evento exato marcado apos `2xx`.
- `py_compile` e `git diff --check` passaram.
- A primeira revisao adversarial recusou a inferencia por grafo e o lock durante HTTP; ambos foram removidos. A segunda recusou fallback pelo runtime; ele tambem foi removido, mantendo o ledger como unica fonte externa do CDR.
- A revisao adversarial final deu `GO`: confirmou origem exclusiva no ledger, baixa do evento exato, bloqueio de reabertura apos `2xx` e ausencia de `runtime_variables` no contrato externo.
- Suites integradas antigas ainda falham antes do codigo alterado pela assinatura removida `trigger_orch(flow_uuid=...)`, gap ja documentado na baseline.
- Deploy e observacao do POST real permanecem pendentes.

## 2026-08-25 — Webhook por tentativa Dialer da mesma sessao

### REQUEST

Preservar a sessao unica de um contato no `send_with_dialer`, mas publicar um webhook para cada CDR de tentativa sequencial enquanto o Dialer esgota suas tentativas.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED` — a sessao `6949` recebeu dois CDRs distintos, mas o segundo POST foi suprimido pelo `2xx` do primeiro, apesar de a segunda chamada pertencer a uma nova tentativa valida da mesma sessao.

### ROOT CAUSE

- O bloqueio de sucesso era por sessao, quando a semantica Dialer e por tentativa/CDR.
- O fallback de hangup recusava retomar uma sessao com webhook confirmado e a persistencia do CDR tambem recusava gravar novo CDR apos esse sucesso.

### CHANGE

- Hangup Dialer pode retomar explicitamente a sessao recente confirmada para processar uma nova tentativa.
- Cada CDR distinto pode disparar o webhook; o `Idempotency-Key` continua determinado por sessao e evento.
- O `discard_reason` do registro em `orch_channel_events` recebe `finish_flow_webhook_dispatched` apos `2xx`, tornando o ledger a marca duravel que suprime somente o replay do mesmo CDR.
- A identidade e o `uniqueid`/`Linkedid` recebido do Dialer; sem ela, o ORCH usa hash deterministico do payload como fallback, sem migration.
- O body externo e inalterado: `session` limpa mais `cdr` cru, sem `runtime_variables`.

### VALIDATION

- Testes focados e de regressao de workflow, ledger, persistencia, trigger e mapper Dialer: `160 passed`.
- Regressao cobre dois CDRs distintos na mesma sessao gerando dois POSTs e a reentrega de CDR marcado gerando zero POST adicional.
- Revisao adversarial auxiliar foi iniciada, mas o client local do agente perdeu permissao para inicializar antes de produzir veredito; a revisao estatica final foi feita localmente. A garantia permanece *at-least-once* no limite entre `2xx` externo e commit local, risco ja registrado em `KNOWN_RISKS.md`.
- Sem migration, filas, beats ou infraestrutura nova.

## 2026-08-25 — Contencao de replay por marcador de CDR

### INCIDENT

O CDR `GW01-1787659477.294612` da sessao `6950` recebeu `2xx`, mas foi reenviado continuamente pelo reconciliador de eventos pendentes.

### ROOT CAUSE

- O SQL de `mark_channel_event_processed` reutilizava `:discard_reason` em `COALESCE` e em `IS NOT NULL` sem tipagem explicita.
- Com `asyncpg`, PostgreSQL recusou a query com `AmbiguousParameterError`; a transacao foi revertida apos o POST externo e o evento permaneceu pendente.

### CONTAINMENT

- O operador autorizou marcar somente o evento `13908` como `finish_flow_webhook_dispatched` em producao, preservando o CDR distinto `13909` pendente.

### FIX

- Tipar `discard_reason` como `TEXT` em ambos os usos da query para que a marca duravel seja persistida apos `2xx`.
- Validacao local focada: `48 passed`, `py_compile` e `git diff --check`.
- Deploy e verificacao de um CDR novo continuam pendentes.

## 2026-08-31 — Materializacao de HSM no ORCH

### REQUEST

Retirar do Target Core a interpretação de flow/card durante o Contact Supplier e fazer o ORCH definir o HSM do contato em foco.

### CLASSIFICATION

`ALPHA_FIX_REQUIRED`; risco crítico de integração e ordem de rollout.

### ROOT CAUSE

O ORCH persistia apenas ANI/`linked_actuator`. O Supplier carregava a definição publicada e tentava inferir o card HSM por `contact.extra.template_name`; no flow `95c0b826-5834-453f-8a20-f80d328b2e57`, a dica do contato e o template do card divergiam e a seleção retornou `hsm=null` após marcar o contato em execução.

### CHANGE

- Os três cards HSM historicamente aceitos materializam texto, log, payload, template, idioma, card e ANI em `contact_list_members.outbound_hsm`.
- ANI, consumo e HSM usam o mesmo savepoint; falha reverte roteamento e segue branch `exception*` ou terminaliza com `whatsapp_hsm_*`.
- Reentrada da mesma sessão/revisão/card reutiliza o HSM pela chave de idempotência e não incrementa novamente o rate limit.
- O nome em `contact.extra` não escolhe o template; o card efetivamente executado é a autoridade.
- O log estruturado `orch.workflow.m2.whatsapp_hsm_preparation_failed` registra falhas sem payload/PII.

### VALIDATION

- `tests/test_workflow_m2_service.py` + `tests/test_orch_sessions_repository.py`: 114 passaram.
- `tests/test_workflow_m2_whatsapp_interactive.py` fora da sandbox: 4 passaram contra PostgreSQL configurado, incluindo branch `exception` para falha HSM.
- `py_compile` e `git diff --check` passaram.
- Migration Target, stack completa, Supplier real e envio externo permanecem pendentes; nenhum deploy foi executado.

## 2026-09-06 — Fixacao da revisao executavel por sessao

### REQUEST

Antes de iniciar a sequencia de novos cards do roadmap comum, impedir que uma sessao em andamento troque de grafo quando uma nova revisao do flow for publicada.

### CLASSIFICATION

`ALPHA_FIX_OPTIONAL` — mudanca pequena e isolada que remove um risco conhecido de cursores inconsistentes sem criar schema, fila ou contrato externo.

### ROOT CAUSE

- O bootstrap ja persistia `revision_id`, `revision_version` e `revision_mode` em `runtime_variables.workflow_v2`.
- O executor M2 ignorava esses campos e chamava novamente o seletor da maior revisao publicada/draft.
- Eventos Dialer ligados ao `finish_flow` e callbacks tardios de `send_with_dialer`/`run_flow` tambem consultavam o grafo corrente.

### CHANGE

- A leitura por `revision_id` exige que a revisao pertença ao mesmo flow.
- M2, eventos dependentes do grafo e callbacks tardios usam a revisao da sessao.
- Sessao legada sem pin recebe a revisao selecionada por atualizacao JSONB atomica e preserva os demais campos do runtime.
- Pin declarado invalido ou inexistente terminaliza com alarme e metrica; nunca executa a revisao corrente como fallback.
- A correlacao tardia fixa o `session_id` esperado entre a leitura do runtime e o update, evitando aplicar o card de uma sessao em outra durante corrida.
- Revisoes publicadas recebem a garantia forte. Draft mutavel continua como limite conhecido do Alpha.

### VALIDATION

- Regressao direcionada de workflow, bootstrap, metricas, dispatcher, eventos de canal, callbacks e repositorios: `177 passed`.
- Dois testes de repositorio executados fora da sandbox contra PostgreSQL configurado confirmaram pin JSONB atomico, preservacao de runtime e isolamento da revisao por flow.
- Casos explicitos cobrem N preservada apos N+1, sessao nova em N+1, pin invalido/inexistente fail-closed e compatibilidade legada.
- Comparacao completa: `origin/main` teve `459 passed, 26 failed`; o branch teve `467 passed, 26 failed`, com as mesmas falhas legadas de assinatura e oito testes novos aprovados.
- Stack local completa reiniciada em terminal dedicado. Smokes reais nos flows `2cb9482a-131e-4b2a-8507-484745661836` e `fea492fb-9420-4690-ba09-bd73dca50717` retornaram `202`, terminaram as sessoes `7283`/`7284` e gravaram nas metricas a mesma revisao fixada no runtime (v26/v16, respectivamente).
- `compileall` e `git diff --check` passaram antes da consolidacao documental.
- O commit funcional `1e3b878` foi integrado pela PR `#142`; o merge `b614a73` foi implantado em `10.1.20.237` e `10.1.20.136` com os `.env` locais preservados e backups de mesmo hash.
- No host `10.1.20.237`, a API e os cinco workers de workflow foram reiniciados de forma gradual; no `10.1.20.136`, somente a API, conforme a topologia ativa observada. Readiness confirmou o schema esperado em ambos e o journal nao apresentou erros nas unidades afetadas.
- O canario dedicado usou o flow `d32bd97e-78ef-4a3f-8541-bc8fe70ffdf0`: a sessao `7285` iniciou na revisao N/v1, pausou, atravessou a publicacao de N+1/v2 e terminou ainda com runtime, cursor e metricas de N. A sessao nova `7286` iniciou e terminou em N+1, tambem com runtime e metricas coerentes.
- A limpeza transacional removeu somente os artefatos do flow canario. A FK `orch_billing_events_source_session_id_fkey`, com `ON DELETE RESTRICT`, exigiu remover primeiro os eventos de billing das duas sessoes; a primeira tentativa foi integralmente revertida e a segunda terminou com zero sessoes, metricas e revisoes do canario. O script temporario foi removido.
- Rollback aprovado sem migration: reverter o commit funcional `1e3b878` e reiniciar gradualmente API/workers de workflow.

## 2026-09-07 — Engine `select_contact_channel`

### REQUEST

Implementar no ORCH o card já publicado no catálogo do Target Core e validá-lo primeiro com o flow canário em `channel`; somente após integração e deploy, incluir o flow na allowlist `person` do Target Core e repetir o E2E.

### CLASSIFICATION

`ALPHA_FIX_OPTIONAL` — mudança contida que permite uma decisão explícita de canal em sessões por pessoa, sem migration, fila ou efeito externo próprio.

### CHANGE

- Em `channel`, a busca permanece presa ao membro e endereço de origem da sessão.
- Em `person`, a busca fica limitada à mesma pessoa, lista e mailing, prioriza `is_primary` e usa o menor ID como desempate.
- A seleção `person` atualiza somente `orch_sessions.entity_address`, sob guards de sessão ativa, escopo e colisão.
- A linha candidata e a sessão são bloqueadas durante a escolha/rebind; `not_found`, `exception` e marcadores incompletos revogam a autorização de comunicação em modo fail-closed.
- A escolha é persistida em `variables.customs[output_var]` e `workflow_v2.selected_contact_channel`, permitindo hidratação consistente nas retomadas.
- Cards de comunicação continuam bloqueados em `person` antes de uma escolha explícita; depois dela, recebem o membro selecionado e permanecem responsáveis por `linked_actuator`.
- Falhas configuracionais ou de persistência seguem `exception*` quando disponível ou terminalizam uma vez com alarme específico.

### VALIDATION PARCIAL

- Testes de configuração, branches, rebind, colisão, retomada, revogação fail-closed, alarmes e integração com o card Dialer: `30 passed` no arquivo dedicado.
- Regressão direcionada de workflow e repositório: `140 passed`; o teste PostgreSQL isolado do seletor também passou.
- Suíte completa: `584 passed, 27 failed`. As 27 falhas continuam nos mesmos node IDs e famílias da baseline já comparada: assinatura legada de `trigger_orch`, expectativas antigas de sessão/out-of-order e invalidação de prepared statement em tabela temporária; nenhuma falha nova foi introduzida.
- Lint dos arquivos alterados/novos, formatação dos arquivos novos e `git diff --check` passaram.
- Stack local completa iniciou com filas `*_f5_local`; os dois smokes encadeados retornaram `202`.
- Após a revisão final, um Uvicorn órfão da checkout principal foi identificado pelo `cwd`, encerrado e substituído pela API desta branch. O smoke canônico repetido gerou as sessões `7503`–`7512`; todas terminaram em `state=3`, cursor final e `next_card_uuid=NULL`. No shutdown, três gerações órfãs de workers/beats `f5_local` foram encontradas e encerradas por PID/PPID; a porta 7777 e a busca pelos consumidores/beat schedules isolados terminaram vazias.
- Sessão canário `7477`, revisão publicada v2, executou `channel -> selected -> api_call -> finish_flow`, terminou sem alarme e preservou membro/endereço. O destino respondeu `200` em uma tentativa com `status=received`.
- Sessão local controlada `7480` executou o mesmo grafo com `session_scope=person`, `person_uuid` e roteamento contextual válidos, sem alarme e com novo `200/status=received`. O mailing canário possui um canal por pessoa; a troca de membro/endereço em `person` foi provada no teste PostgreSQL isolado.
- E2E `person`, rollout e deploy permanecem pendentes. A allowlist não foi alterada antes de a engine estar disponível nos servidores ORCH.
