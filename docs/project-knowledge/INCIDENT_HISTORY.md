# Historico de Incidentes

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

### Acao recomendada

1. Preservar contagens, task IDs, hostnames, commits e logs antes de intervir.
2. Tratar primeiro a amplificacao silenciosa e a maquina de estados com `MINIMUM SAFE CHANGE`.
3. Definir com o responsavel funcional a semantica de `confirmar`, `encerrar` e `falar_com_atendente`.
4. Implementar/validar `live` somente contra contrato real e E2E que observe o handoff no destino.
5. Validar cada branch de API separadamente; nao considerar HTTP 200 prova de handoff.

### Acoes executadas nesta investigacao

Somente consultas read-only a PostgreSQL/Celery, leitura de codigo, historico Git e revisao adversarial. Nenhuma sessao, flow, fila, worker ou dado de producao foi alterado.
