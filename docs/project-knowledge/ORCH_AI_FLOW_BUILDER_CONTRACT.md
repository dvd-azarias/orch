# ORCH AI Flow Builder — contrato do MVP textual

## Escopo

Superfície de control-plane isolada para criar rascunhos
`mode=orchestration`. Não participa do runtime das sessões e não publica flows.
A UI conversa somente com seu BFF; as credenciais internas do ORCH nunca são
entregues ao navegador.

## Rotas

Base autenticada: `/v1/orch/{workspace_uuid}/flow-builder`.

- `POST /sessions`: inicia uma conversa `intent=create`;
- `GET /sessions/{session_id}`: recarrega estado e mensagens;
- `POST /sessions/{session_id}/assist`: recebe `expected_version` e texto,
  consulta o catálogo real, planeja, compila e persiste o turno atomicamente;
- `POST /sessions/{session_id}/compile`: endpoint técnico de compilação
  explícita preservado para diagnóstico/control-plane;
- `POST /sessions/{session_id}/draft`: cria idempotentemente o rascunho no
  Target Core e persiste `flow_uuid`/checksum;
- não existe rota `publish`.

Todos os requests exigem credencial interna dedicada. Rotas que representam um
usuário exigem também `X-Orch-Flow-Builder-Actor`. O workspace deve estar ativo,
na allowlist e corresponder ao schema transacional selecionado.

## Concorrência e idempotência

`expected_version` impede que abas/turnos concorrentes sobrescrevam a conversa.
O draft usa slug determinístico `orch-ai-{builder_session_id}`. Se a resposta
da criação for incerta e o Target responder conflito numa repetição, o ORCH
recupera o mesmo flow por slug e confirma a identidade em
`builder_metadata.builder_session_id` antes de aceitá-lo.

## Segurança do planejador

- somente JSON aderente ao schema Pydantic é aceito;
- IDs, cards, branches e opções precisam existir no catálogo do workspace;
- `ref_id` e a `definition` são gerados pelo compilador, nunca pelo modelo;
- defaults de parâmetros sensíveis são omitidos e planos anteriores têm campos
  sensíveis redigidos antes de compor o prompt;
- prompt e credenciais não são registrados nos logs;
- anexos/imagens são recusados neste gate.

## Timeout isolado

O planejador usa `ORCH_FLOW_BUILDER_LLM_TIMEOUT_SECONDS` (padrão 60 segundos),
sem alterar `OTIMA_LLM_API_TIMEOUT_SECONDS`. Assim, a geração estruturada pode
esperar uma resposta maior sem alongar o limite dos cards de IA existentes.

## Rollback

`ORCH_FLOW_BUILDER_ENABLED=false` remove a superfície de uso sem afetar flows ou
sessões normais. As tabelas aditivas podem permanecer. O rascunho eventualmente
já criado no Target continua um draft comum, revisável e removível pelos meios
existentes do Target.
