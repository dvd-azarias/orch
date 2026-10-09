# Rollout global do Supplier V2 e runtime v86 — 2026-10-09

## Decisão homologada

O Supplier V2 deixa de ser segmentado por workspace/flow e passa a ficar
disponível globalmente para os cards novos. Essa decisão não migra cards
legados:

- `dialer` e `send_with_dialer` continuam Supplier V1;
- `send_with_dialer_handoff` continua sendo o único card de voz Supplier V2;
- a imagem v86 suporta os dois caminhos e não escolhe o contrato pelo número da
  imagem;
- o contrato vem do catálogo Target Core e é validado novamente no Kerberos e
  no runtime do discador.

Classificação: `ALPHA_FIX_OPTIONAL`, com benefício operacional claro e mudança
cirúrgica, reversível por feature flag.

## Gates globais

| Aplicação | Gate principal | Gate global novo | Fallback ao desligar |
| --- | --- | --- | --- |
| ORCH voz | `DIALER_SUPPLIER_V2_ENABLED` | `DIALER_SUPPLIER_V2_ALLOW_ALL_CONTEXTS` | allowlists de workspace/flow |
| ORCH SMS/RCS | `CHANNEL_SUPPLIER_V2_ENABLED` | `CHANNEL_SUPPLIER_V2_ALLOW_ALL_CONTEXTS` | allowlists de workspace/flow |
| ORCH Metrics | `ORCH_METRICS_EVENTS_ENABLED` | `ORCH_METRICS_EVENTS_ALLOW_ALL_WORKSPACES` | allowlist de workspace |
| Target SMS/RCS | `CONTACT_SUPPLIER_CHANNEL_DISPATCH_V2_ENABLED` | `CONTACT_SUPPLIER_CHANNEL_DISPATCH_V2_ALLOW_ALL_CONTEXTS` | allowlists de workspace/flow |
| ORCHESTRATOR filas humanas | sincronizador AMI ativo | `ORCHESTRATOR_SYNC_HUMAN_LIVE_ALL_FLOWS_ENABLED` | `ORCHESTRATOR_SYNC_HUMAN_LIVE_FLOW_UUIDS` |

Todos os gates globais usam default `false`. O master flag correspondente
continua obrigatório. UUIDs inválidos continuam falhando fechado. As
allowlists existentes devem ser preservadas durante a primeira ativação para
rollback rápido.

O multilane permanece independente e explicitamente delimitado por
`ORCH_DIALER_MULTILANE_V2_*` no ORCH e `TARGET_DIALER_MULTILANE_V2_*` no Target.

## Ordem segura de promoção

1. Promover Target Core e ORCH com os gates globais ainda desligados.
2. Promover ORCHESTRATOR e configurar a descoberta global de filas humanas.
3. No control plane `.136`, tornar v86 a imagem default e remover overrides de
   imagem antigos; preservar os masters do Supplier V2.
4. Ativar o gate global Target em rolling `.239` e `.249`, com health e smoke
   entre os nós.
5. Ativar os gates globais ORCH no nó executor `.237`; aplicar configuração de
   callback nos nós HTTP somente quando consumida por eles.
6. Recriar os containers dinâmicos do `.143` em lotes controlados, observando
   Kerberos, gateway, Supplier V1 e Supplier V2.
7. Validar um card legado e um card novo em workspaces distintos antes de
   concluir 100%.

## Provas obrigatórias

- card legado em v86 chama exclusivamente `/v1/contact-supplier/...`;
- `send_with_dialer_handoff` chama exclusivamente
  `/v2/contact-supplier/dialer-cycles`;
- mismatch card/contrato é recusado pelo Kerberos/runtime;
- a descoberta global de filas humanas ignora `send_with_dialer` e destinos
  BOT;
- reconciliadores ORCH percorrem todos os workspaces concluídos no modo global;
- Metrics publica fato real de workspace fora da allowlist anterior;
- SMS/RCS registra, executa e recebe callback fora das allowlists anteriores;
- multilane continua limitado à sua allowlist própria.

## Rollback

Desligar os gates globais e reiniciar somente os consumidores correspondentes.
As allowlists preservadas retomam imediatamente o escopo canário. Reverter a
imagem default no `.136` e recriar containers no `.143` é etapa separada e só é
necessária se houver falha específica do runtime v86; não é necessária para
restringir o Supplier V2 novamente.
