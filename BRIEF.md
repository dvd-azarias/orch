# BRIEF — Particionamento Assíncrono por TEAR

## Decisão

Adotar modelo **TEAR-first** para filas assíncronas do ORCH:

- fila por TEAR como padrão;
- fairness por workspace dentro do mesmo TEAR;
- exceções de fila dedicada por workspace apenas quando formalmente justificadas.

## Motivação

- reduzir complexidade operacional (evitar explosão de filas por workspace);
- escalar por classe de SLA/capacidade;
- melhorar isolamento e previsibilidade de backlog;
- manter observabilidade clara por classe de serviço.

## Diretriz de implementação

- Convenção de nomes de fila:
  - `orch_<dominio>_<etapa>_<tear>`
  - exemplo: `orch_fileapp_ingest_gold`
- Sempre carregar `workspace_uuid` no payload/header.
- Aplicar fairness por workspace:
  - prefetch baixo;
  - limite de concorrência por worker;
  - rate limit/backoff por workspace quando necessário.

## Exceções permitidas

- fila dedicada por workspace apenas em casos de:
  - SLA crítico contratual;
  - incidente operacional com contenção temporária.
- toda exceção deve ter:
  - justificativa registrada;
  - prazo de revisão;
  - plano de retorno ao modelo TEAR-first.

## Não objetivos

- não adotar fila permanente por workspace como padrão de arquitetura.

## Métricas de sucesso

- latência p95/p99 por TEAR;
- taxa de erro por TEAR;
- backlog por TEAR;
- sinal de starvation por workspace dentro do TEAR.
