# Channel Dispatch V2 — SMS e RCS

## Objetivo

Ativar o envio real dos cards `send_with_sms` e `send_with_rcs` sem alterar a
Supplier V1 e sem transformar `linked_actuator` em autorização suficiente para
um disparo.

Classificação: `ALPHA_FIX_OPTIONAL`, com ativação exclusivamente canária.

## Invariantes

1. O ORCH nunca chama diretamente os provedores de SMS ou RCS.
2. `linked_actuator` continua sendo apenas o marcador de roteamento.
3. O envio só é autorizado por uma intenção durável, cifrada, vinculada a:
   workspace, sessão, flow, revisão, card, lista, membro, canal e sequência.
4. A Supplier V1, suas rotas, tabelas, workers e contratos não são alterados.
5. A ativação exige feature flag e allowlists de workspace e flow nos dois lados.
6. Tokens, mensagem e destinatário não podem aparecer em logs nem em colunas
   de auditoria em texto claro.
7. Como os provedores atuais não aceitam uma chave de idempotência do cliente,
   uma falha ambígua após o início do POST nunca gera reenvio automático.

## Fluxo de saída

1. O runtime chega ao card e valida o canal em foco.
2. Na mesma transação, o ORCH:
   - marca o membro com `linked_actuator=sms|rcs`;
   - renderiza o envelope do provedor;
   - cifra o envelope com Fernet;
   - grava a intenção em `runtime_variables.workflow_v2.channel_dispatch_v2`;
   - bloqueia a sessão no card.
3. Somente depois do commit, uma task dedicada registra a intenção em
   `POST /v2/contact-supplier/channel-dispatches`.
4. A Supplier v2 valida revisão/card/membro/marcador, persiste idempotentemente
   e enfileira o dispatch numa fila exclusiva.
5. O worker faz claim transacional antes do HTTP e chama o provedor.
6. Resposta explícita válida vira `accepted`; recusa explícita vira `failed`;
   timeout, perda de conexão ou morte após claim vira `uncertain`.

## Idempotência e segurança

- A chave idempotente é determinística sobre a identidade da intenção e o hash
  do conteúdo canônico antes da cifra; a aleatoriedade do Fernet não altera a
  identidade de um replay legítimo.
- O banco da Supplier v2 possui unicidade por chave e por intenção.
- Um claim expirado em `dispatching` é terminalizado como `uncertain`; ele não
  é reenviado automaticamente.
- O envelope é cifrado no ORCH e só é decifrado na Supplier v2.
- A chave deve ser exclusiva deste contrato e compartilhada por configuração,
  nunca pelo Git ou pelo definition do flow.
- Respostas persistidas são reduzidas a campos seguros: identificador, status,
  descrição e código do provedor.

## Gate 1 — entrega atual

- registro e outbox idempotentes;
- envio real de SMS e RCS;
- prova de aceite imediato do provedor;
- estados `pending`, `dispatching`, `accepted`, `failed` e `uncertain`;
- v1 intocada;
- sessão permanece bloqueada após o aceite.

O aceite HTTP não significa entrega ao destinatário. Para RCS, `status=V`
significa que a solicitação foi considerada válida, não que foi entregue/lida.

## Gate 2 — contrato aprovado

Classificação: `ALPHA_FIX_OPTIONAL`, opt-in e canária. O Gate 1 permanece
inalterado quando a flag específica de callbacks está desligada.

### Autoridade e callback público

- o ORCH continua sendo a autoridade do grafo e gera, por intenção, URLs
  públicas opacas e assinadas;
- os callbacks apontam diretamente para o webhook do ORCH, nunca para o
  Runner genérico nem para uma URL fixa do definition;
- SMS recebe três URLs por dispatch: DLR, MO e status;
- RCS recebe duas URLs por mensagem, nos campos oficiais
  `url_callback_mo` e `url_callback_status`;
- a Supplier V2 continua sendo a única emissora. Ela apenas transporta as URLs
  já materializadas no envelope cifrado e não tenta interpretar o grafo;
- nenhuma URL, token, telefone, mensagem ou credencial de provedor é registrada
  em logs.

### Correlação e segurança

O token de callback é um envelope compacto assinado por HMAC, derivado da
mesma chave exclusiva do Channel Dispatch V2. Ele contém somente identidade
operacional não sensível: workspace, flow, sessão, revisão, card, canal e
sequência. Não contém telefone, pessoa, lista, mensagem ou credencial.

Ao receber um callback, o ORCH:

1. valida formato, versão e assinatura em tempo constante;
2. fixa o workspace trazido pelo token e localiza exatamente a sessão;
3. confere flow, revisão, card, canal e sequência contra a intenção ativa ou
   seu histórico durável;
4. normaliza o evento conforme o contrato oficial do provedor;
5. persiste idempotentemente em `orch_channel_events` antes de enfileirar a
   retomada;
6. responde replay como sucesso idempotente e nunca cria sessão nova a partir
   de callback.

O identificador externo do provedor é usado na deduplicação. Quando ausente,
o callback é rejeitado; correlação por telefone ou por "sessão ativa mais
recente" é proibida.

### Semântica SMS

- o card expõe somente a saída `next`/`Próximo`; DLR, MO e status nunca são
  configurados no canvas, pois o ORCH gera as três URLs por dispatch;
- o primeiro callback válido libera o card e todo callback normalizado é
  preservado no inbox como `callback/sms_event`;
- a condição posterior decide sobre `data.status`, que pode ser `sent`,
  `delivered`, `not_delivered`, `response`, `failed`, `status` ou `dlr`;
- a saída do SMS fornece uma chave opaca ligada a sessão, revisão, card,
  canal e sequência. `wait_for_event.correlation_key` impede que outro
  disparo da mesma sessão satisfaça a espera;
- callback que chega antes do wait permanece elegível; ao retornar da condição
  para o mesmo wait, o timeout conserva o prazo absoluto original;
- mensagens usadas nos canários são explicitamente textos de teste;
- callbacks estáticos deixam de existir no catálogo. O SMS V2 falha fechado se
  o Gate de callbacks internos não estiver habilitado para o contexto.

### Semântica RCS

- `sent`/`queued` são somente telemetria e não liberam o card;
- `delivered` e `read` liberam somente quando correspondem ao
  `completion_event` configurado;
- mensagem ou arquivo recebido libera `response`;
- `unavailable`, `failed` e `expired` seguem seus branches próprios;
- `timeout_seconds` arma `frozen_until`; ao vencer sem evento conclusivo, o
  runtime segue exclusivamente pelo branch `timeout`;
- callback recebido antes de o worker observar o bloqueio permanece no ledger
  e é consumido na retomada, sem janela de perda.

### Concorrência, replay e rollback

- persistência do callback precede o enqueue;
- o reconciliador existente de `orch_channel_events` cobre o gap
  commit → enqueue;
- sessão encerrada, desassociada ou com identidade divergente não é reaberta;
- eventos duplicados não repetem branch, envio ou avanço de card;
- rollback operacional: desligar a flag de callbacks, retirar o flow da
  allowlist e preservar ledger/envelopes para auditoria. Supplier V1 não é
  alterada.

Até o Gate 2 ser ativado e homologado no canário, o aceite HTTP imediato do
provedor continua sem liberar a sessão.

## Rollback

1. retirar o flow da allowlist ou desligar `CHANNEL_SUPPLIER_V2_ENABLED` no ORCH;
2. desligar `CONTACT_SUPPLIER_CHANNEL_DISPATCH_V2_ENABLED` no Target Core;
3. drenar/parar as filas exclusivas;
4. manter tabelas e runtime para auditoria; não executar downgrade destrutivo.

## Retomada do objetivo maior

Após homologar SMS/RCS no canário, reconfigurar e testar o fluxo completo
`c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`, com waits reduzidos para 2–5 minutos.
