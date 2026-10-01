# Ponte de tabulação Runner v5 → ORCH

## Estado e classificação

`ALPHA_FIX_REQUIRED`. O objetivo é receber a tabulação que o Live já entrega ao
Runner v5 e acordar exatamente a sessão ORCH que originou a mensagem WhatsApp,
sem usar telefone como chave e sem alterar o endpoint genérico de trigger.

## Persistência e correlação

A migration `0027_create_runner_orch_tabulation_bridge` cria, em cada workspace:

- `orch_runner_session_links`: vínculo durável entre sessão Runner, mensagem do
  provider e sessão ORCH;
- `orch_runner_tabulation_events`: receipt idempotente da tabulação, inclusive
  quando ela chega antes do vínculo.

O bind procura `messages[].context.id` em `orch_channel_events.event_id`. O
resultado só é aceito quando existe exatamente uma sessão ORCH ativa. Zero
resultados retorna `not_found`; mais de um ou divergência retorna `conflict`.
Não há fallback por telefone. Bind e tabulação usam o mesmo advisory lock da
sessão Runner, fechando a corrida evento-antes-do-vínculo.

A tabulação é anexada como `callback/tabulation` somente à sessão vinculada. A
sessão só volta a `state=0` quando está bloqueada no `wait_for_event` com a mesma
fonte e resultado. Sessão encerrada ou `unassigned` é marcada como `ignored` e
nunca é revivida. Replay do mesmo evento na mesma sessão Runner é idempotente.

## API interna

- `POST /v1/orch/{workspace_uuid}/runner-bridge/bind`;
- `POST /v1/orch/{workspace_uuid}/runner-bridge/tabulations`.

Ambos exigem `x-client-id` e `x-client-secret` dedicados, configurados por
`RUNNER_ORCH_BRIDGE_CLIENT_ID` e `RUNNER_ORCH_BRIDGE_CLIENT_SECRET`. Ausência ou
divergência é `401`; as credenciais não reutilizam o trigger público e não são
registradas em logs.

## Implantação segura

1. Aplicar `python -m app.cli migrate-all` uma vez contra o banco compartilhado
   e conferir a versão `0027` em todos os workspaces.
2. Configurar o mesmo par de credenciais no ORCH e no Target Core.
3. Reiniciar ORCH e validar autenticação dos endpoints com payload inofensivo.
4. Manter o Target em `off` até worker e fila dedicados estarem prontos.
5. O Target é a única autoridade de rollout: primeiro allowlist TRC, depois
   expansão e por fim `all`. O ORCH não possui allowlist casual própria.
6. Só remover o webhook manual do `finish_flow` do BOT depois do canário E2E.

Rollback é desligar o modo no Target. Não apagar receipts nem links: eles são a
trilha de auditoria e não produzem execução sem novas chamadas autenticadas.

## Canário e retorno ao objetivo principal

O primeiro canário é o workspace TRC
`11497cd6-0332-49bb-a9f9-de0addca0114`, fluxo ORCH
`96cd32a7-8736-4312-a871-3e3eb3a3c5c7` e fluxo BOT
`6dec25e2-3f2a-4331-956d-9d5e313f5118`. Validar separadamente `ACEITE`,
`RECUSA` e `IMPRODUTIVA`, replay, evento antes do vínculo e sessão já encerrada.
Concluído esse gate, retomar a homologação dos canários e do fluxo completo que
foi pausada para esta correção.
