# Rastreamento de Jornadas

## Objetivo

Disponibilizar uma superfície de suporte somente leitura para responder, sem
acesso direto ao banco:

- quantas sessões e visitas passaram por cada card e branch de um flow em um
  período;
- quais sessões de uma pessoa pertencem ao flow no período;
- qual revisão foi realmente executada;
- qual caminho uma sessão percorreu e onde ela está ou terminou.

Classificação: `ALPHA_FIX_OPTIONAL`. A entrega não modifica dispatcher,
executor, callbacks, sessões, métricas ou definições. Ela projeta dados já
persistidos para reduzir investigação manual e risco operacional de suporte.

## Arquitetura e confiança

```text
navegador
  -> BFF da Gestão de Extensões (HTTP Basic + workspace validado)
  -> ORCH Observability (credencial dedicada servidor-servidor)
  -> SELECTs no schema do workspace
```

O navegador nunca recebe a credencial do ORCH nem acessa o banco. O BFF aceita
somente três rotas `GET` e uma lista fechada de query strings. O ORCH permanece
fail-closed quando uma das duas variáveis de credencial está ausente.

Headers internos:

- `X-Orch-Observability-Client-Id`
- `X-Orch-Observability-Client-Secret`

## Contrato HTTP

Base: `/v1/orch/{workspace_uuid}/observability`

### Visão agregada

`GET /flows/{flow_uuid}/journey?from=<ISO>&to=<ISO>&revision_id=<UUID opcional>`

Retorna o canvas estrutural da revisão, revisões observadas, sessões únicas,
visitas, erros, latências e tráfego por branch. As revisões observadas são
derivadas das métricas de card ocorridas no período, não da data de criação da
sessão.

### Sessões de uma pessoa

`GET /flows/{flow_uuid}/sessions?from=<ISO>&to=<ISO>&person_uuid=<UUID>&limit=50&cursor=<opcional>`

O filtro usa o UUID canônico da pessoa. Para `session_mode=channel`, resolve o
vínculo histórico de `contact_list_member_id` gravado no runtime da sessão. Para
`session_mode=person`, usa o mesmo vínculo e mantém fallback pelo identifier da
person para sessões antigas. Não há comparação global por número de telefone;
isso evita misturar pessoas diferentes que compartilhem o mesmo endereço.

Os identificadores e endereços retornados são mascarados.

### Traço individual

`GET /flows/{flow_uuid}/sessions/{session_uuid}/trace`

Reconstrói o canvas da revisão pinada na sessão e as métricas de card em ordem
cronológica. Expõe nós e edges visitados, posição atual ou terminal e informa
quando o início do histórico foi truncado pelo limite seguro.

## Dados deliberadamente não expostos

- parâmetros de componentes;
- tokens, credenciais ou headers de integrações;
- `runtime_variables`;
- payloads de callbacks;
- definição bruta do flow;
- identifier e endereço completos na resposta do ORCH.

A projeção do canvas contém somente `ref_id`, tipo, descrição, posição e
branches estruturais.

## Guardrails de produção

- somente métodos `GET`;
- cada request abre a transação PostgreSQL como `READ ONLY` antes da validação
  do workspace, impedindo escrita também no nível do banco;
- período máximo padrão de 168 horas;
- no máximo 100 sessões por página;
- no máximo 2.000 passos recentes por trace;
- `statement_timeout` padrão de 5 segundos;
- `search_path` selecionado por workspace ativo;
- cursor opaco para paginação;
- nenhum endpoint de escrita ou exportação em massa;
- nenhuma dependência nova e nenhuma migration.

Configuração ORCH:

```dotenv
ORCH_OBSERVABILITY_CLIENT_ID=
ORCH_OBSERVABILITY_CLIENT_SECRET=
ORCH_OBSERVABILITY_MAX_WINDOW_HOURS=168
ORCH_OBSERVABILITY_STATEMENT_TIMEOUT_MS=5000
ORCH_OBSERVABILITY_MAX_TRACE_STEPS=2000
```

Configuração do BFF:

```dotenv
ORCH_OBSERVABILITY_BASE_URL=
ORCH_OBSERVABILITY_CLIENT_ID=
ORCH_OBSERVABILITY_CLIENT_SECRET=
```

O mesmo par de credenciais deve ser provisionado nos dois lados fora do Git.

## UI

A funcionalidade fica em:

`Extensões -> Orquestração -> Rastreamento de Jornadas`

- flow + período: visão agregada;
- flow + pessoa + período: lista de sessões;
- sessão selecionada: canvas da revisão realmente executada, com o caminho
  percorrido destacado, nós não visitados esmaecidos e posição atual/terminal
  evidente.

A fonte da UI continua sendo `GOHP-LAB/target-extensions-ui`; o deploy segue
`DIALING_MANAGEMENT_UI_RUNBOOK.md`.

## Rollout e rollback

1. Implantar o ORCH com credenciais ainda ausentes: a rota fica `503` e não
   interfere no runtime.
2. Provisionar a credencial dedicada no ORCH e reiniciar somente sua API.
3. Validar os três `GETs` com um flow/sessão canário.
4. Provisionar o mesmo par no BFF.
5. Implantar a UI pelo script canônico e executar o smoke visual.

Rollback da UI é a troca atômica para a release anterior. Rollback do ORCH é a
revisão anterior ou a remoção das credenciais, que desabilita a superfície sem
afetar a execução de flows.

## Continuidade do objetivo maior

Esta entrega é uma adaptação temporária de curso para melhorar suporte. Depois
de homologada, o trabalho deve retornar ao flow completo
`c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`, revisando sua definição contra os
contratos finais dos componentes antes de retomar os testes E2E.
