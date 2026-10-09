# Ativação de flow/workspace no Supplier V2

Runbook operacional para habilitar um novo flow de orquestração, especialmente
quando ele combina Discador V2, SMS, RCS e WhatsApp. Não contém valores de
segredo; esses valores permanecem exclusivamente nos ambientes.

## Regra central

Ativação de voz e ativação multicanal são gates independentes. Um flow pode
discar corretamente e ainda não estar autorizado a registrar ou executar SMS e
RCS. Nunca usar o sucesso do primeiro como prova do segundo.

Também há duas aplicações envolvidas:

1. o ORCH materializa a intenção de voz ou canal e retoma a sessão;
2. o Target Core Supplier V2 persiste e executa o dispatch real de SMS/RCS.

O WhatsApp não usa o outbox `channel-dispatches` do Target Core. Ele possui
seleção, envelope, provedor e callbacks próprios no caminho já existente. Não
adicionar um flow às allowlists SMS/RCS esperando habilitar WhatsApp.

## Topologia canônica

| Host | Aplicação/papel | Serviços relevantes |
| --- | --- | --- |
| `10.1.20.237` | execução ORCH | `orch-celery-worker_01..05`, `orch-celery-dialer-supplier-v2-worker`, `orch-api` |
| `10.1.20.239` | Target Core Supplier + nó HTTP ORCH | `target-core-supplier`, `celery_contact_supplier_v2_channel_dispatch`, `orch-core-api` |
| `10.1.20.249` | Target Core Supplier + nó HTTP ORCH | `target-core-supplier`, `celery_contact_supplier_v2_channel_dispatch`, `orch-core-api` |

`.136` é control plane Kerberos/gateway e `.143` hospeda os containers
dinâmicos do discador. Eles não substituem nenhum dos gates desta tabela.

## Matriz feature -> configuração -> consumidor

| Capacidade | Aplicação/host | Configuração | Processo a recarregar |
| --- | --- | --- | --- |
| Discador Supplier V2 | ORCH `.237` | `DIALER_SUPPLIER_V2_ENABLED`; canário por allowlists ou global por `DIALER_SUPPLIER_V2_ALLOW_ALL_CONTEXTS` | `orch-celery-worker_01..05` e `orch-celery-dialer-supplier-v2-worker` |
| Materialização SMS/RCS | ORCH `.237` | `CHANNEL_SUPPLIER_V2_ENABLED`; canário por allowlists ou global por `CHANNEL_SUPPLIER_V2_ALLOW_ALL_CONTEXTS`, chave/id de envelope e configuração de callback | `orch-celery-worker_01..05` |
| Execução real SMS/RCS | Target Core `.239/.249` | `CONTACT_SUPPLIER_CHANNEL_DISPATCH_V2_ENABLED`; canário por allowlists ou global por `..._ALLOW_ALL_CONTEXTS`, mapa de chaves, endpoints e timeout | `target-core-supplier` e `celery_contact_supplier_v2_channel_dispatch` |
| Callback público SMS/RCS | ORCH `.237/.239/.249` | `CHANNEL_SUPPLIER_V2_CALLBACKS_ENABLED` e as mesmas allowlists de workspace/flow | API ORCH de cada nó; `.239` antes de `.249` e `.237` somente quando sua API mudou |
| WhatsApp | ORCH/Target no caminho existente | política do destinatário no card, template/ANI, membro escolhido e callbacks do provedor | somente o processo que consumir a configuração efetivamente alterada |

Nos nós `.239/.249` existem dois projetos e dois `.env` diferentes:

- `/etc/gohp/target-core/.env`: autoriza e executa SMS/RCS;
- `/etc/gohp/orch/.env`: aceita os callbacks SMS/RCS distribuídos pelo pool
  HTTP.

Atualizar somente um deles deixa a jornada incompleta.

## Invariante V1 x V2

O rollout global não migra cards existentes. `dialer` e `send_with_dialer`
continuam produzindo contrato Supplier V1. Somente
`send_with_dialer_handoff` produz contrato Supplier V2. A imagem v86 executa os
dois contratos e valida a combinação card/contrato; portanto trocar a imagem
default para v86 não altera, por si só, o contrato de um card legado.

As flags `*_ALLOW_ALL_CONTEXTS` removem apenas a segmentação por workspace e
flow do caminho V2 já identificado. Elas não mudam o seletor de contrato.

## Checklist para novo flow no mesmo workspace

Em modo global, os passos de inclusão nas allowlists de workspace/flow abaixo
não são necessários. Mantê-las preenchidas é recomendado para rollback rápido:
ao desligar `*_ALLOW_ALL_CONTEXTS`, elas voltam a ser o escopo efetivo.

1. Confirmar revisão publicada, `session_mode`, cards usados, Perfil de
   Discagem publicado, destino e branches conectadas.
2. Se houver `send_with_dialer_handoff`, adicionar o `flow_uuid` às allowlists
   `DIALER_SUPPLIER_V2_*` do `.237`.
3. Se houver `send_with_sms` ou `send_with_rcs`, adicionar o `flow_uuid`:
   - às allowlists `CHANNEL_SUPPLIER_V2_*` do ORCH `.237`;
   - às allowlists `CONTACT_SUPPLIER_CHANNEL_DISPATCH_V2_*` do Target Core
     `.239/.249`;
   - às allowlists `CHANNEL_SUPPLIER_V2_*` das APIs ORCH `.239/.249`.
4. Se houver WhatsApp, confirmar separadamente:
   - template/ANI válidos;
   - política de destinatário compatível com o membro atual; em flow `person`,
     `reuse_current_phone` permite reutilizar o telefone selecionado;
   - callback do provedor alcança a API ORCH compatível no pool.
5. Em cada `.env`, preservar backup restrito e comparar o conteúdo excluindo
   apenas a chave modificada. Nunca copiar o arquivo inteiro de outro host.
6. Reiniciar somente os consumidores da matriz, em rolling. Nos pares
   `.239/.249`, concluir health do `.239` antes de tocar o `.249`.
7. Validar processo novo; ler a linha do `.env` sem restart não prova a
   configuração efetiva.
8. Gerar uma sessão nova por novo vínculo. Não reconstruir manualmente envelope
   ou intenção de uma sessão que atravessou o card antes do gate ser ativado.

## Complemento para workspace novo

Além dos passos de flow, acrescentar o `workspace_uuid` em todas as allowlists
correspondentes, preservando os workspaces existentes. Confirmar previamente:

- schema e migrations do ORCH e Target Core;
- chaves de envelope equivalentes no ORCH e Target Core;
- endpoints dos provedores e credenciais presentes, sem exibi-los;
- filas dedicadas com consumers ativos;
- callback público e autenticação idênticos nos três nós HTTP ORCH;
- Perfil de Discagem, calendários, limites e bindings publicados no novo
  workspace.

## Provas mínimas por capacidade

### Voz

Na mesma sessão, exigir:

1. intenção `workflow_v2.dialer_supplier_v2`;
2. ciclo `ready`/claim real;
3. chamada selecionada;
4. callback correlacionado e branch correto.

### SMS/RCS

Na mesma sessão, exigir:

1. parada no card correto e intenção `workflow_v2.channel_dispatch_v2`;
2. registro Target Core aceito, sem `channel_dispatch_v2_disabled`;
3. outbox `accepted` e POST do provedor observado;
4. callback público HTTP 202;
5. ledger da mesma sessão/card e retomada prevista.

### WhatsApp

Exigir membro/endereço esperados, HSM único, evento `sent`, evolução real
(`delivered`, `read`, `failed` ou resposta) e retomada da mesma sessão. O teste
de SMS/RCS não comprova WhatsApp.

## Diagnóstico rápido por sintoma

| Sintoma | Primeira hipótese a provar |
| --- | --- |
| Card Dialer bloqueia sem intenção/ciclo | allowlist Dialer no ORCH `.237` |
| Voz funciona, mas SMS/RCS não cria intenção | allowlist Channel no ORCH `.237` ou worker sem reload |
| Intenção existe, registro retorna 503 `channel_dispatch_v2_disabled` | allowlist Target Core `.239/.249` |
| Provedor aceitou, mas callback recebe 404 intermitente | allowlist/flag divergente entre APIs ORCH `.237/.239/.249` |
| WhatsApp não encontra elegível após voz/SMS | política do card e membro selecionado; não é gate Target Core SMS/RCS |
| Sessão antiga continua falhada após habilitar | criar sessão nova; não sintetizar envelope retroativamente |

## Rollback

Desligar primeiro a flag global correspondente e reiniciar, em rolling, os
mesmos processos da matriz; as allowlists canárias preservadas retomam o
controle. Se necessário, restaurar somente os backups dos `.env` alterados.
Retirar o flow de uma allowlist em modo canário impede novos
registros; não apaga outboxes, ciclos, tentativas, mensagens ou callbacks já
persistidos. Tratamento de registros existentes é operação separada e exige
evidência e autorização próprias.

## Canário de referência

Em 2026-09-30, o flow `34496bfd-478c-4030-8382-ec0416e1efd0` (Bradesco TRC
003), workspace `ba7eb0ec-e565-447c-8c11-8f870cf72a60`, demonstrou os gates
em sequência:

- sem a allowlist Dialer no `.237`, não houve ciclo de voz;
- após ativá-la, voz e callback `no_answer` funcionaram;
- sem a allowlist Channel no `.237`, não houve intenção SMS;
- após ativá-la, a intenção foi materializada;
- sem a allowlist do Target Core, o registro falhou com
  `channel_dispatch_v2_disabled`/HTTP 503;
- a ativação foi então alinhada no Target Core `.239/.249` e nas APIs ORCH de
  callback `.239/.249`, com health HTTP 200 e zero restarts.

O E2E final requer uma sessão nova e prova de envio/callback; configuração
saudável isoladamente não substitui essa evidência.
