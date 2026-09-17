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

## Gate 2 — posterior, antes da retomada automática

- documentar e capturar os callbacks oficiais de SMS e RCS;
- criar ledger idempotente de eventos externos;
- correlacionar callback à intenção sem expor segredo;
- mapear DLR/MO/status e RCS delivered/read/response/expired;
- entregar evento terminal ao ORCH por outbox;
- retomar exatamente a sessão/card/revisão e branch corretos;
- implementar timeout verificável para o branch correspondente.

Até o Gate 2, nenhuma resposta imediata do provedor libera a sessão.

## Rollback

1. retirar o flow da allowlist ou desligar `CHANNEL_SUPPLIER_V2_ENABLED` no ORCH;
2. desligar `CONTACT_SUPPLIER_CHANNEL_DISPATCH_V2_ENABLED` no Target Core;
3. drenar/parar as filas exclusivas;
4. manter tabelas e runtime para auditoria; não executar downgrade destrutivo.

## Retomada do objetivo maior

Após homologar SMS/RCS no canário, reconfigurar e testar o fluxo completo
`c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`, com waits reduzidos para 2–5 minutos.
