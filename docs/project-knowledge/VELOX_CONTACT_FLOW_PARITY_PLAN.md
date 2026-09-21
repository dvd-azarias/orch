# Paridade Velox com os cards de contato

Auditoria e plano de substituição preparados em 2026-09-19 como Gate 8 de
`CONTACT_LIFECYCLE_HOMOLOGATION_PLAN.md`.

## Objetivo e limites

Reproduzir no workspace HighComm o comportamento de negócio hoje distribuído
entre `CREATE_CUSTOMER` e `ACIONADOR`, usando os cards homologados de contato e
sem modificar os flows originais da Velox.

Esta etapa:

- não copia credenciais, payloads pessoais ou segredos;
- não altera definitions, listas, sessões ou dados do workspace Velox;
- não reutiliza o discador legado no substituto;
- não cria componentes novos quando os cards existentes atendem ao contrato;
- mantém explícita a retomada posterior do flow completo
  `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.

## Revisões congeladas

| Flow Velox | Flow UUID | Revisão publicada analisada | Versão |
|---|---|---|---:|
| `CREATE_CUSTOMER` | `160c13c8-3eb4-40ba-99e7-d3bdddec885b` | `0fcd83d8-0f36-4094-9f1c-09c43ebcf8a7` | 19 |
| `ACIONADOR` | `652ee631-888e-46f9-843e-d80543051801` | `ed58ceea-3c32-41c7-9f47-8ee247c7b61f` | 55 |

A coleta foi feita em transação PostgreSQL `READ ONLY` no schema do workspace
Velox. Os relatórios intermediários foram sanitizados antes de sair do host; o
documento registra somente contratos e contagens sem PII.

## Comportamento real observado

### `CREATE_CUSTOMER`

Topologia publicada:

```text
code_editor -> generate_file -> finish_flow
```

O flow recebe um webhook genérico, normaliza até três telefones para dígitos e
adiciona o DDI brasileiro quando ausente. O código analisado possui 34 linhas,
1.115 caracteres e fingerprint
`16be13b804a0ef814303b7e7bd999823f432ed1668b01a732de149eba6f82707`.

Depois, o flow exporta por SFTP uma linha CSV com:

- `id_atendimento`;
- `nome_cliente`;
- `tipo_pessoa`;
- `nr_placa`;
- `tp_alerta`;
- `nr_telefone_01`, `nr_telefone_02` e `nr_telefone_03`;
- `nr_documento`;
- `IdClienteFullTrack`;
- `IdVeiculoFullTrack`;
- `id_alerta`;
- `protocolo_manubot`.

Na fotografia da revisão atual, os três cards haviam sido percorridos com
sucesso aproximadamente 40 mil vezes. O próprio flow não cadastra pessoa nem
canal; ele usa arquivo como transporte para o importador.

Há um detalhe legado que não deve ser copiado cegamente: a expressão de origem
de `protocolo_manubot` contém um espaço depois de `payload.`. O substituto deve
usar o caminho semanticamente correto e provar o valor em payload controlado.

### Importação intermediária

O template efetivo é:

- id interno `6`;
- UUID `8ba2e495-ec77-4161-a29c-f096fbd8f35e`;
- nome `template_monitoramento_05`.

O de/para confirmado é:

| Campo de entrada | Destino atual |
|---|---|
| `id_alerta` | `CONTACT_IDENTIFIER` |
| `nome_cliente` | `CONTACT_FULL_NAME` |
| `nr_telefone_01..03` | `CHANNEL_PHONE` |
| demais campos | `EXTRA_GENERIC` |

Portanto, a identidade de negócio atual da pessoa é `id_alerta`, e não
`nr_documento`. Mudar isso durante a portabilidade criaria pessoas com outra
chave e não seria paridade. Qualquer mudança futura para documento deve ser uma
decisão de produto/migração separada.

Cada arquivo observado produz uma `source_list` `PROCESSED` com exatamente uma
linha processada e sem canal ausente. Em seguida essa lista é vinculada ao
`ACIONADOR`. Na fotografia de 2026-09-19:

- `CREATE_CUSTOMER` tinha somente dois vínculos históricos próprios, ambos
  inativos;
- `ACIONADOR` acumulava `40.156` vínculos distintos de mailing;
- `5.157` ainda estavam ativos;
- os mailings mais recentes eram listas unitárias criadas poucos segundos
  antes do vínculo;
- nos oito dias observados, o volume diário ficou entre centenas e cerca de
  duas mil listas unitárias.

Isso comprova que a divisão atual em dois flows é uma ponte técnica criada
antes dos cards de contato, não uma necessidade real de batch.

### `ACIONADOR`

O flow está em `session_mode=channel` e usa o card legado
`send_with_dialer`. Sua topologia funcional é:

```text
send_with_dialer
  rejected/ busy/ machine/ no_answer/ failed/ invalid_number
    -> set_variables(status)
    -> api_call(callback)
    -> finish_flow

  answered
    -> set_variables(status=ATENDIDA)
    -> run_flow(AGV_ALERTA_DIGITAL)
    -> hangup/tabulacao
    -> api_call(callback)
    -> finish_flow
```

O callback usa `session_external_id`, `protocolo_manubot`, o status normalizado
e dados da tabulação. Falha do callback percorre um segundo `api_call` de
observabilidade e termina. A revisão atual possuía dezenas de milhares de
execuções e continuava ativa durante a auditoria; por isso os originais
permanecem intocados.

## Decisão: um substituto no HighComm

Para este caso, será criado **um único flow em `person`** no HighComm. Isso não
revoga o modelo de ingestão e acionamento desacoplados: cargas para uso futuro
ou em lote continuam exigindo dois flows. O caso Velox é diferente porque cada
webhook já provoca imediatamente uma importação unitária e um vínculo ao
acionador.

O flow único é profissional neste cenário porque:

1. o webhook já representa uma unidade individual de trabalho;
2. `create_contact` adota a pessoa na mesma sessão;
3. `manage_contact_channels` persiste os três telefones sem criar sessões;
4. `source_list_membership(current_flow_operational)` vincula uma lista canária
   estável, materializa o membro no próprio flow e mantém
   `sessions_created=0`;
5. `select_contact_channel` escolhe explicitamente o telefone elegível;
6. somente `send_with_dialer_handoff` marca o atuador;
7. nenhuma lista por evento, CSV intermediário ou SFTP é necessário.

O suporte necessário já existe nos runtimes implantados:

- ORCH: `80e65c4` está contido no merge implantado `e946afe`;
- Target Core: PR `#534`, merge `e474e69`, confirmado nos hosts `.239` e
  `.249`.

## Desenho do substituto

Nome proposto: `Canário Velox — Alerta Digital v2`.

```text
webhook person/unbound
  -> create_contact(upsert por payload.id_alerta)
  -> manage_contact_channels(upsert dos telefones 01..03)
  -> source_list_membership(
       selected,
       active,
       current_flow_operational
     )
  -> select_contact_channel(
       voice,
       first_eligible,
       respect_dial_rule
     )
  -> send_with_dialer_handoff
       ├─ releases não atendidas -> status -> callback -> finalizar
       ├─ answered -> BOT -> aguardar hangup/tabulação -> callback -> finalizar
       ├─ limit_reached -> status controlado -> callback -> finalizar
       └─ exception -> término de atenção sem retry infinito
```

### Mapeamento de pessoa

`create_contact`:

- `person_action=upsert`;
- `identifier={{payload.id_alerta}}`;
- política inicial `overwrite_non_null`;
- `full_name={{payload.nome_cliente}}`;
- extras: `id_atendimento`, `tipo_pessoa`, `nr_placa`, `tp_alerta`,
  `nr_documento`, `IdClienteFullTrack`, `IdVeiculoFullTrack`, `id_alerta` e
  `protocolo_manubot`;
- campos vazios preservam os dados existentes, como homologado no Gate 7.

`manage_contact_channels`:

- operação `upsert`;
- até três itens `voice` derivados de `nr_telefone_01..03`;
- prioridades `1`, `2` e `3`;
- somente o primeiro telefone não vazio fica marcado como principal;
- telefone ausente não pode produzir canal vazio;
- normalização fica a cargo do card, não de `code_editor` duplicado.

### Lista operacional

O canário usará uma única lista HighComm, criada especificamente para a
homologação. A lista não é o identificador da pessoa nem uma sessão; ela é a
âncora operacional necessária ao Supplier V2.

O card de lista deve usar:

- `mailing_source=selected`;
- `membership_state=active`;
- `membership_purpose=current_flow_operational`;
- `person_uuid={{contact_action.person_uuid}}`.

Esse contrato vincula/materializa no flow atual com `skip_orch_sessions=true`.
A mesma sessão continua; nenhuma sessão filha é criada.

### Discagem e callback

O substituto usará somente `send_with_dialer_handoff` e Supplier V2. O card
legado `send_with_dialer` e a Supplier V1 permanecem intocados.

Antes da publicação são obrigatórios:

- um Perfil de Discagem canário no HighComm;
- destino BOT canário no mesmo workspace, ou término controlado temporário no
  branch `answered`;
- endpoint de callback de teste, nunca o endpoint real da Velox;
- limite baixo de canais e tentativas;
- lista sem contatos de produção.

O payload do callback de paridade deve conservar a semântica de
`session_external_id`, `protocolo_manubot`, `status`, descrição, resposta e
tabulação. Credenciais e URL reais só entram depois da homologação funcional.

## Cards reutilizados e gaps

| Necessidade | Componente existente | Situação |
|---|---|---|
| Criar/localizar/atualizar pessoa | `create_contact` | atende |
| Persistir três telefones | `manage_contact_channels` | atende |
| Ancorar na lista/flow sem fan-out | `source_list_membership` operacional | atende |
| Escolher telefone submetido à regra | `select_contact_channel` | atende |
| Entregar ao discador V2 | `send_with_dialer_handoff` | atende |
| Normalizar release | branches + `set_variables` | atende |
| Notificar sistema externo | `api_call` | atende |
| Entregar para BOT | configuração do handoff/run flow existente | atende, depende do flow destino |
| Esperar tabulação/evento | `wait_for_event` quando necessário | atende |

Nenhum card novo foi identificado como necessário. As pendências são apenas
configurações do canário e decisões de destino, não gaps de engine.

## Matriz de homologação

1. Payload completo com três telefones: pessoa, três canais, uma associação,
   uma sessão e um único linked actuator.
2. Payload com um telefone: nenhum canal vazio e seleção do único elegível.
3. Repetição idêntica de `id_alerta`: `unchanged` sem duplicidade.
4. Mesmo `id_alerta` com nome/extras atualizados: somente campos não vazios são
   alterados.
5. Mesmo `id_alerta` com telefone adicional: canal novo sem duplicar os
   anteriores.
6. `id_alerta` ausente: branch de atenção, zero pessoa, canal, lista e
   acionamento.
7. Todos os telefones vazios/inválidos: branch controlado antes do atuador.
8. Mesmo telefone em dois `id_alerta` distintos: duas pessoas válidas, sem
   bloqueio global.
9. Releases `busy`, `machine`, `no_answer`, `rejected`, `invalid_number`,
   `failed`, `answered` e `limit_reached`.
10. Callback com sucesso, erro e timeout sem hot loop.
11. Repetição concorrente do mesmo payload: uma pessoa, canais idempotentes e
    no máximo um ciclo de discagem efetivo conforme a correlação da sessão.
12. Auditoria final: revisão fixada, zero branch órfão, zero alarme inesperado,
    zero sessão filha e ausência de escrita no workspace Velox.

## Gates de execução

- [x] Congelar as duas revisões Velox.
- [x] Inventariar definitions, branches e efeitos sem segredos.
- [x] Confirmar a ponte lista unitária por evento.
- [x] Mapear o template de importação para pessoa, canais e extras.
- [x] Decidir pelo flow único para este caso em tempo real.
- [x] Confirmar runtime do vínculo operacional no ORCH e catálogo nos dois
      Target Core.
- [x] Criar lista e flow canários no HighComm pelas APIs oficiais.
- [x] Validar o draft com o `422` atual antes de publicar.
- [x] Publicar sem endpoint de cliente e sem contato de produção.
- [x] Executar a matriz controlada sem discagem real.
- [x] Executar discagem real somente após autorização específica.
- [ ] Comparar resultado com as revisões Velox congeladas.
- [ ] Registrar UUIDs, revisions, sessões, branches e contagens.
- [ ] Encerrar Gate 8.
- [ ] Retomar obrigatoriamente o flow completo
      `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.

## Evidência do preflight sem discagem

Em 2026-09-20, o substituto foi publicado no HighComm como flow
`12fd033e-5793-4b00-95d1-dfe8867de67f`, revisão v2
`a9bb2891-80db-4731-a3b9-bbb0e0cf3c14`. A lista canária usada foi
`60333a24-447d-4d32-9ab5-f0f6fbefcd31` (`mailing_id=1141`).

As sessões `8461` a `8468` provaram criação, cinco canais, idempotência,
identificador ausente, canais ausentes, endereço compartilhado por pessoas
distintas, atualização com canal adicional e conflito intrapayload. O caminho
operacional produziu no máximo uma pessoa por identificador, materializou o
número esperado de membros, selecionou o primeiro elegível e encerrou antes do
Dialer. Em todas as provas: `sessions_created=0`, zero sessão filha, zero
`linked_actuator`, zero alarme e nenhuma escrita no workspace Velox.

As linhas 9, 10 e a parcela de discagem concorrente da linha 11 da matriz
continuam pendentes porque exigem habilitar explicitamente o gate
`enable_dialer=true`. Não fazê-lo sem autorização específica.

## Evidência da primeira discagem real

Em 2026-09-20, uma autorização específica permitiu executar o gate com o
payload `gate8-real-20260920-002`. A sessão `8487` selecionou o membro `11517`,
marcou `linked_actuator=dialer` e registrou exatamente um ciclo/tentativa na
Supplier V2. O `service_dialer` chamou o PBX, que retornou `CONGESTION`, causa
34, antes de tocar. O evento foi normalizado como `technical_failure`, consumiu
a única tentativa do Perfil e encerrou a pessoa por
`person_attempt_limit_reached`.

O callback terminal foi entregue ao ORCH, mas a retomada expôs o gap
`contact_member_scope_not_found`: a sessão nasceu sem membro, adotou a pessoa e
selecionou o canal durante o próprio fluxo, portanto sua `entity` continuou
sendo uma correlação sintética. A correção de handoff já integrada pela PR
`#200` não resolve sozinha a hidratação posterior. O patch seguinte deve usar
somente `workflow_v2.selected_contact_channel` válido para recuperar o membro
exato sem exigir igualdade entre essa correlação e o identificador da pessoa;
lista, mailing e endereço da sessão continuam obrigatórios. A nova prova real
deve usar outra pessoa e outra tentativa, nunca alterar o ledger já consumido.

## Rollback

O substituto nasce em flows/lista canários novos no HighComm. Rollback consiste
em desvincular a lista canária, suspender o novo flow e remover qualquer
credencial de teste. Os dois flows Velox permanecem inalterados e continuam
sendo a referência operacional até a homologação completa e uma decisão
separada de corte.
