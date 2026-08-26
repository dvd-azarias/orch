# Historico de Incidentes

## 2026-08-26 — FileApp aguardava rescue após status avançado no Target Core

`STATUS`: ROOT CAUSE CONFIRMED / FIX IMPLEMENTED / ROLLOUT PENDING

`SEVERITY`: high

`CLASSIFICATION`: `ALPHA_FIX_REQUIRED`

`WORKSPACE`: `253148c7-a85f-42a3-bc8b-5ffd9d885efe`

`FLOW`: `652ee631-888e-46f9-843e-d80543051801`

### Evidencia e causa

Os callbacks S3 foram aceitos e persistidos em cerca de centenas de milissegundos, mas alguns arquivos permaneceram em `monitoramento/upload`. Os workers concluíram o pipeline como falha no `step5_put_field_mappings` em menos de um segundo: o PUT retornou HTTP 200 com mailing `INGESTING` ou `PROCESSED`, enquanto o ORCH exigia exatamente `READY_TO_INGEST`.

O código do Target Core confirmou a corrida: `sync_mapping_status` muda para `READY_TO_INGEST` e publica `source_list.ingest` quando existe template. O worker pode avançar o mailing antes da serialização da resposta. Portanto, `INGESTING`/`PROCESSED` são progresso válido, não falha. O rescue retomava o receipt após 600 segundos e explicava a latência observada.

### Correção

O ORCH passa a aceitar `INGESTING`/`PROCESSED` no passo 5 e omite o POST de import nesses estados para não duplicar a ingestão já iniciada. `READY_TO_INGEST` preserva o caminho anterior; demais estados permanecem falha. A associação assíncrona continua responsável por aguardar o estado final antes do vínculo.

### Validação

Foram adicionados testes de regressão para `INGESTING`, `PROCESSED` e estado regressivo. A regressão FileApp passou com 73 testes; a stack local completa ficou pronta e o smoke canônico dos dois flows passou. Rollout e E2E de produção permanecem pendentes.

## 2026-08-24 — `linked_actuator` aplicado em membro de outra lista

`STATUS`: CONFIRMED / FIX IMPLEMENTED BEHIND DEFAULT-OFF FLAG / RUNTIME VALIDATION PENDING

`SEVERITY`: critical

`CLASSIFICATION`: `ALPHA_FIX_REQUIRED`

`WORKSPACE`: `ba7eb0ec-e565-447c-8c11-8f870cf72a60` (`Highcomm`)

`FLOW`: `3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17` (`Demo - Discador Preditivo`)

### Evidencia

- O flow estava ativo e selecionava a revisao publicada v5; o primeiro card era `send_with_dialer`.
- O contato `30392286855` possuia tres membros ativos em listas diferentes.
- As sessoes `6928` e `6937` carregavam no payload a lista `dc7dc1c1-2c98-42e9-a788-5d186f458daa` e mailing `1115`.
- O membro esperado era `10655`, mas o runtime das duas sessoes registrou atribuicao ao membro `10687`, mais novo e pertencente a outra lista/mailing.
- A metrica de `6937` confirmou `blocked_send_with_dialer`; portanto o card executou. A falha nao foi ausencia de execucao, mas selecao da linha errada.
- O membro `10687` recebeu `linked_actuator=dialer` e `ani=1147371485`; `10655` permaneceu com ambos nulos.

### Causa

Os tres caminhos de resolucao de contato relevantes usam somente `entity = contact_identifier`, filtram membros ativos e escolhem o mais novo globalmente. O payload preserva `contact_list_id`/`mailing_id`, mas esses campos sao ignorados pelo roteamento e pelo contexto injetado no workflow.

### Blast radius

Uma agregacao read-only encontrou 38 identificadores com mais de um membro ativo, 113 linhas envolvidas e pelo menos 26 sessoes em que a lista do membro roteado divergia da lista declarada no payload. Foram observadas 23 sessoes Dialer e 3 WhatsApp interativo em tres flows. A contagem e conservadora porque exige payload e metadata de routing persistidos.

### Revisao adversarial

A revisao independente confirmou a causa como deterministica e apontou o mesmo defeito em Dialer, WhatsApp e `fetch_contact_runtime_context_for_session`. Recomendou resolucao contextual compartilhada, validacao cruzada dos identificadores e fallback legado somente quando o evento nao trouxer identidade da lista.

### Acoes executadas

- Investigacao de producao permaneceu read-only; nenhuma sessao, membro, flow, fila ou worker foi alterado.
- Implementada resolucao contextual unica para contexto, Dialer, WhatsApp e WhatsApp interativo.
- `contact_list_member_id` e o seletor primario; lista e mailing presentes sao validadores cruzados. Lista precede mailing quando o ID do membro esta ausente.
- Seletor explicito invalido/incompativel encerra a sessao com falha e alarme; update perdido por desatribuicao concorrente tambem terminaliza, em vez de bloquear o card sem efeito externo.
- Fallback legado permanece somente para payload sem qualquer seletor e a ativacao depende de feature flag default-off.
- Duas revisoes adversariais encontraram falta de alarme inline, janela concorrente e consulta pouco indexavel; os tres pontos foram corrigidos antes da validacao.
- Testes unitarios focados: 105 passaram. O teste PostgreSQL com tabelas temporarias passou separadamente. Validacao E2E de stack ainda pendente.

### Incidente durante validacao

A tentativa de stack completa nao conta como validacao da correcao. `dev_phase_stack.sh status` reportava tudo down, mas processos `f5_local` orfaos desde 16:30 BRT continuavam ativos. A nova subida adicionou consumidores e encontrou warnings de hostname duplicado; dispatch, FileApp rescue e generate scan foram processados antes da contencao.

Todos os processos Uvicorn/Celery deste checkout foram encerrados. A porta 7777 e a lista de processos ficaram vazias; nenhum node listado pelo broker permaneceu consumindo filas `f5_local`. As metricas globais continuaram crescendo pelo storm de producao preexistente, portanto nao foram usadas como criterio de contencao local. Nao houve alarme `contact_member_*`. O generate job observado executou `no_rows`, sem envio de linhas. Nenhum smoke HTTP foi disparado.

Esse evento revelou R21 e impede declarar E2E de stack validado. Permanecem validos o teste PostgreSQL com tabelas temporarias e a comprovacao read-only no caso real.

Durante a auditoria do generate job, uma consulta excessivamente ampla exibiu configuracao sensivel no terminal do agente. O valor nao foi persistido na documentacao nem no codigo, mas a credencial SFTP correspondente deve ser rotacionada como follow-up operacional.

## 2026-08-24 — Retry storm do flow ORQUESTRADOR

`STATUS`: CONTAINED

`SEVERITY`: critical

`CLASSIFICATION`: `ALPHA_FIX_REQUIRED`

`WORKSPACE`: `91c85c54-cd88-4aed-88e0-7eb720674f5d` (`Comercial`)

`FLOW`: `0e378237-4a61-4d5f-89f3-b07b594df38f` (`ORQUESTRADOR`)

### Impacto observado

- Tres sessoes permaneciam no primeiro card, sem `last_card_uuid`.
- Entre 2026-08-11 10:42 BRT e 2026-08-24 11:35 BRT foram persistidos 1.136.779 alarmes do flow.
- As falhas dominantes eram `api_call sem URL valida` e `Branch 'false' nao encontrado e o condition nao possui branch de exception`.
- Havia quatro eventos WhatsApp pendentes na terceira sessao.
- As falhas continuavam sendo registradas durante a investigacao.

### Gaps confirmados na definicao

1. Flow `draft`, sem revisao publicada, mas aceito pelo runtime por fallback para draft.
2. Condition inicial compara `contact.identifier` com `1`, `2` ou `3`, sem branch `false` e sem edge de exception, apesar de `has_exception_branch=true`.
3. Cards `ENVIAR EMAIL` e `ENVIA SMS` possuem URL vazia; o primeiro possui apenas branch `success`.
4. A exception edge do SMS nao trata `api_call_missing_url`, pois excecoes de `api_call` sao relancadas pelo executor atual.
5. O child flow `Pre-vendas automatizado` tambem esta `draft`, sem revisao publicada.
6. O mapping template existe, mas mapeia tanto `QuantidadeParcelas` quanto `Cpf` para o campo central `CONTACT_IDENTIFIER`.
7. `set_variables` nao possui instrucoes; e um no-op, sinal de configuracao incompleta.

### Amplificacao operacional

O dispatcher atual faz claim dentro de savepoint sob uma transacao externa que nao recebe commit explicito antes do publish Celery. O enqueue e externo ao PostgreSQL. Em falha permanente, o estado/cursor da sessao nao progride e a sessao volta a ser elegivel.

A evidencia observada e fortemente compativel com esse mecanismo, mas a investigacao nao capturou task IDs e logs do dispatcher suficientes para atribuir cada retry a um claim especifico.

### Possivel drift de workers

Foram observados quatro workers workflow nas filas de producao: um hostname `@136_01` e tres `@237_03`, `@237_04`, `@237_05`. Depois do commit que tornou conditions estritas, continuaram coexistindo o erro novo de branch ausente e o comportamento antigo de fallback para a primeira edge, que alcanca a API call vazia.

Workers com revisoes diferentes sao a explicacao mais provavel. O horario de deploy/restart e o commit efetivo de cada processo permanecem `UNKNOWN`.

### Acao recomendada

1. Preservar logs, task IDs, hostnames, commits e contagens antes de intervir.
2. Isolar o dispatcher/reconciliador ou as sessoes afetadas por procedimento aprovado; nao assumir que `status=draft` ou `is_active=false` bloqueia execucao.
3. Corrigir e validar o grafo fora do ambiente compartilhado: fallback da condition, URLs/branches, child flows e mapping.
4. Corrigir a amplificacao de erro permanente com commit duravel do claim e politica de terminalizacao/backoff/DLQ.
5. Executar E2E separado para as tres branches, incluindo efeitos externos e SQL final.

### Acoes executadas nesta investigacao

- As sessoes `256` e `257`, unicas elegiveis ao dispatcher no momento da primeira intervencao, foram terminalizadas com `state=3`, `ended_at`, `frozen_until` de protecao e marcador `workflow_v2.terminal_failure`.
- O update usou guard por workspace, flow, IDs, `state=0`, `ended_at IS NULL` e cursor presente; qualquer divergencia abortaria a transacao.
- A primeira contagem estabilizou em 1.153.974 alarmes.
- Durante a validacao, a stack local `f5_local` permaneceu ativa alem do esperado. Seu dispatcher estava escopado ao workspace de teste, mas `CELERY_RECONCILE_PENDING_EVENTS_WORKSPACE_UUID` estava vazio; o reconciliador global encontrou a sessao WhatsApp `263`, `state=2`, e passou a reenfileira-la.
- A sessao `263` falhava de forma deterministica em `api_call_missing_url`. Ela foi terminalizada com guard exato por workspace, flow, ID, `state=2`, `ended_at IS NULL` e cursor presente as 16:50 BRT.
- A stack local foi parada pelo script oficial. O ultimo alarme ocorreu as 16:50:14 BRT, antes da terminalizacao de `263`; apos 60 segundos sem processos locais, a contagem permaneceu em 1.154.025 e nenhuma sessao elegivel existia.
- Nenhum alarme, metrica, flow, fila ou worker foi apagado ou alterado.

### Correcao preparada

- `condition_branch_not_mapped` passa a ser falha terminal, sem capturar outras excecoes.
- A sessao recebe `state=3`, `ended_at`, `next_card_uuid=NULL` e diagnostico em `workflow_v2.terminal_failure`.
- O caminho Celery persiste um unico alarme e commita a transacao de sucesso terminal.
- Tasks atrasadas ignoram somente sessoes terminais que possuam marcador explicito de falha; sessoes `state=3` normais de WhatsApp/Dialer continuam processaveis.
- Duas revisoes adversariais foram executadas. A primeira encontrou risco de regressao no guard terminal amplo; a implementacao foi restringida e a segunda revisao nao encontrou achados acionaveis.
- `api_call_missing_url` permanece fora desta correcao funcional por ser um caso distinto do comportamento aprovado. O risco e a evidencia foram preservados para decisao separada.

## 2026-08-24 — Retry storm silencioso e handoff ausente no flow Demo WhatsApp Outbound

`STATUS`: ACTIVE WHEN OBSERVED

`SEVERITY`: critical

`CLASSIFICATION`: `ALPHA_FIX_REQUIRED`

`WORKSPACE`: `ba7eb0ec-e565-447c-8c11-8f870cf72a60` (`Highcomm`)

`FLOW`: `4d81d73b-dfee-43b8-9c82-d3c52207941f` (`Demo - WhatsApp - Outbound`)

### Impacto observado

- A revisao publicada v13 estava ativa, com 14 componentes e 25 branches estruturalmente conectadas.
- Sete sessoes GenericApp `state=0` permaneciam no card `send_whatsapp_template` e eram reexecutadas aproximadamente a cada dois segundos.
- Ate 2026-08-24 15:28 BRT, o flow acumulava 4.389.386 metricas de executor `success/blocked_send_whatsapp_interactive`, sem qualquer alarme do flow.
- As sete sessoes usavam entidades geradas, sem telefone real correlacionavel; nao havia evento WhatsApp capaz de acorda-las.
- A resposta `confirmar` da sessao `6927` chegou ao ORCH, atualizou a variavel, concluiu a `api_call` com HTTP 200 e foi finalizada em `component_not_supported:live`. Na fotografia de 15:27 BRT, nao havia sessao nao terminal para o telefone consultado pelo operador.

### Gaps confirmados

1. `blocked_send_whatsapp_interactive` e persistido como sucesso, mas nao pertence a `BLOCKING_RUNNING_STOP_REASONS`; a sessao nao recebe a transicao defensiva para `state=1`.
2. O claim do dispatcher ocorre sem commit externo explicito antes do publish Celery, permitindo que enqueue sobreviva enquanto o claim volta a `state=0`.
3. Alarmes nao cobrem esse loop de sucesso; a tempestade e visivel apenas em metricas/volume operacional.
4. O runtime de `main` e do branch atual nao suporta `live`; alcancar o card termina a sessao em vez de realizar handoff.
5. O commit isolado `bd461a5` nao e ancestral de `main`/HEAD e nao implementa o side effect de handoff ou mirror descrito em sua propria documentacao.
6. As branches `success`, `error` e `exception` da API de resposta convergem no mesmo card `live`; o resultado da API nao altera o comportamento subsequente.

### Hipoteses sem contrato confirmado

- `encerrar` tambem encaminha para `live`; isso parece semanticamente incorreto, mas o comportamento desejado nao foi fornecido.
- O quick reply `falar_com_atendente` e convertido para `falar_atendente` no payload externo; a compatibilidade do consumidor nao foi verificada.
- `limit_reached` e convertido para `limit reached`; a compatibilidade do consumidor nao foi verificada.
- Consumidores compartilhados, beats duplicados ou workers com revisoes distintas podem ampliar o volume, mas task IDs e commits dos processos nao foram correlacionados.

### Revisao adversarial

A revisao independente confirmou a cadeia `state=0 -> scan -> claim sem commit -> enqueue -> bloqueio sucesso -> nova elegibilidade` como explicacao dominante, mas recusou atribuir todas as 4.389.386 execucoes ao dispatcher sem task IDs/logs. Tambem identificou a omissao independente do stop reason na maquina de estados.

A analise do commit `bd461a5` refutou a hipotese de que bastaria integra-lo: para eventos comuns ele retorna branch nula, nao envia handoff e nao chama destino externo; como o resolver usa a primeira edge, este grafo tenderia a finalizar sem atendimento.

## 2026-08-25 — Payload duplicado e eventos Dialer pendentes no `finish_flow`

`STATUS`: PATCH REVISED / LOCAL VALIDATION PASSED

`SEVERITY`: high

`CLASSIFICATION`: `ALPHA_FIX_REQUIRED`

`WORKSPACE`: `ba7eb0ec-e565-447c-8c11-8f870cf72a60`

`FLOW`: `3d2f3ce2-f943-48c6-94f0-cfb4f22bdd17`

`SESSIONS`: `6945` / `1d4ae14d-a25b-437d-a84a-2454c00c6a37` e `6946` / `d17715b5-cac2-446b-bdcb-6f478c6b1442`

### Evidencia confirmada

- O webhook funcional chegou ao destino com HTTP 200, mas carregou `runtime_variables` inteiro e repetiu callback/payload em varios niveis.
- A mesma sessao recebeu dois eventos Dialer com `uniqueid` e `DialerActionID` distintos em oito segundos, apesar do contrato funcional de uma chamada/desfecho por sessao.
- Houve dois envios do webhook terminal para a sessao: o segundo body continha o resultado do primeiro dispatch e `cdr=null`.
- Os eventos `13902` e `13903` permaneceram com `processed_at=NULL`; o reconciliador continuou acionando a sessao terminal com `no_next_card`.
- Depois do primeiro patch de higienizacao, a sessao `6946` recebeu os eventos `13904` e `13905`. O primeiro POST foi confirmado sem CDR; o segundo `finish_flow` foi suprimido pelo sucesso ja persistido.
- O evento `13904` foi recebido as `10:07:18.905 UTC`, processado as `10:07:18.927 UTC` e o webhook saiu as `10:07:18.961 UTC`. O codigo relia a sessao, mas continuava buscando CDR no runtime local e substituia o JSONB completo antes do dispatch.

### Correcao preparada

- Contrato explicito `session` + `cdr`, com contato normalizado uma vez dentro de `session` e sem `runtime_variables`.
- Usar o payload cru de um unico registro pendente de `orch_channel_events`; fluxo Dialer sem CDR nao envia `null`.
- Depois de `2xx`, processar somente o evento utilizado. Evento tardio e auditado individualmente e sessao com webhook confirmado nao pode ser reaberta pelo fallback temporal.
- Nenhuma migration, fila, beat ou worker adicional.

### Risco residual

O envio permanece best-effort dentro da transacao. Crash entre o `2xx` externo e o commit local ainda pode repetir o POST; o destino deve continuar idempotente. A semantica upstream dos dois `DialerActionID` distintos observados na mesma sessao permanece desconhecida.

### Acao recomendada

1. Preservar contagens, task IDs, hostnames, commits e logs antes de intervir.
2. Tratar primeiro a amplificacao silenciosa e a maquina de estados com `MINIMUM SAFE CHANGE`.
3. Definir com o responsavel funcional a semantica de `confirmar`, `encerrar` e `falar_com_atendente`.
4. Implementar/validar `live` somente contra contrato real e E2E que observe o handoff no destino.
5. Validar cada branch de API separadamente; nao considerar HTTP 200 prova de handoff.

### Acoes executadas nesta investigacao

Somente consultas read-only a PostgreSQL/Celery, leitura de codigo, historico Git e revisao adversarial. Nenhuma sessao, flow, fila, worker ou dado de producao foi alterado.

## 2026-08-24 — Amplificacao e gaps observados apos cutover para `10.1.20.237`

`STATUS`: ACTIVE WHEN OBSERVED / INFRASTRUCTURE STABLE

`SEVERITY`: critical para amplificacao; high para health e wrapper

`CLASSIFICATION`: `ALPHA_FIX_REQUIRED`

### Baseline do cutover

- Os dezenove servicos esperados no novo host estavam ativos/enabled e sem restart de unit: API, tres beats e quinze workers.
- Nenhuma fila ORCH tinha mensagem pronta; cada fila tinha cinco consumers.
- O host antigo permaneceu somente com a API ORCH, conforme restricao temporaria do proxy.
- A validacao do roteamento contextual passou em runtime: 46 assignments novos sem divergencia e o caso Dialer alvo convergiu para membro/lista/mailing corretos.

### Gaps confirmados

1. `blocked_send_whatsapp_interactive` continuava reclamando sessoes `state=0` em tres workspaces e gerando metricas/logs sem alarme.
2. Os tres beats publicavam reconciliacoes FileApp iguais; dois tambem publicavam pending-channel reconcile.
3. `/health/celery` considera qualquer worker do broker compartilhado e `/health/ready` nao cobre Celery.
4. SIGTERM de child process pode ser mascarado por `UnboundLocalError` no `finally` de `_advance_session_task`.
5. O custo acumulado ja era material: aproximadamente 199,4 milhoes de linhas e mais de 90 GB em metricas; 234 MB de journal em cerca de 70 minutos no novo host.

### Acoes executadas

Somente consultas read-only, inspecao de systemd/journal, passive queue declare, leitura de codigo e revisao adversarial. Nenhuma intervencao de runtime ou dado foi realizada.

### Correcao preparada

No branch `fix/blocked-whatsapp-interactive-loop`, o dispatcher passou a tratar `blocked_send_whatsapp_interactive` como bloqueio em execucao e a persistir `state=1`. Teste de regressao e 103 testes focados passaram; revisao independente deu `GO`. O incidente permanece aberto ate deploy e comprovacao de que as sessoes quentes deixam de gerar novas tasks depois da primeira execucao corrigida.
