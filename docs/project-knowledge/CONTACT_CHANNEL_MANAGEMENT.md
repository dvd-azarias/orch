# Gerenciamento genérico de canais do contato

Contrato do Gate D da homologação do ciclo de vida de contatos, aprovado em
2026-09-18. A entrega é `ALPHA_FIX_OPTIONAL`: adiciona uma capacidade isolada,
sem migration, fila, endpoint ou acionamento externo.

## Norte

O card `manage_contact_channels` existe para administrar os endereços de uma
pessoa já resolvida. Ele não identifica a pessoa, não cria lista ou membro
operacional, não cria sessão e não define `linked_actuator`.

Ordem segura para uma jornada com atuação na mesma sessão:

```text
create_contact ou identidade_person
  -> manage_contact_channels
  -> source_list_membership
     (finalidade: Disponibilizar para uso neste fluxo)
  -> select_contact_channel
  -> atuador explícito
```

O card é exclusivo de `session_mode=person`. O modo `channel` permanece
inalterado e ancora a sessão no endereço que a originou.

## Envelope

- `person_uuid`: UUID literal ou template, normalmente
  `{{contact.person_uuid}}`;
- `operation`: `upsert|deactivate`;
- `channels`: lista JSON com 1 a 100 objetos;
- cada objeto usa `type`, `address` e opcionalmente `label`, `priority` e
  `is_primary`;
- tipos canônicos: `voice`, `whatsapp`, `sms`, `rcs`, `email`;
- `output_var`: variável em `variables.customs`, padrão `contact_channels`.

Branches: `changed`, `unchanged`, `not_found`, `conflict` e `exception`.

## Semântica

- Telefone: somente dígitos; o prefixo brasileiro `55` é removido quando o
  valor tiver 12 ou 13 dígitos.
- E-mail: `trim` e lowercase.
- Chave idempotente: pessoa + tipo + endereço normalizado.
- `upsert` cria, reativa ou atualiza metadados sem duplicar.
- `deactivate` marca `state=inactive`, `is_valid=false` e
  `is_reachable=false`; não há exclusão física.
- A operação é atômica. Se uma desativação pedir algum canal inexistente, nada
  é alterado e a saída é `not_found`.
- Apenas um canal ativo pode ser principal por pessoa. Ao desativar o
  principal, o próximo ativo por prioridade é promovido.
- O mesmo endereço pode pertencer a pessoas diferentes. Não existe trava
  global de negócio por telefone ou e-mail.

`persons` ainda possui uma projeção legada globalmente única em
`primary_channel_type + primary_channel_value`. O card serializa essa
atribuição e a preenche somente quando o endereço não estiver projetado em
outra pessoa. Uma colisão nessa projeção não rejeita o canal: o estado canônico
por pessoa permanece em `persons.channels`, e a saída informa
`primary_projection_applied=false`.

## Integração com listas

`ensure_person_in_source_list` deve respeitar `state=inactive`, copiar
`is_valid`, `is_reachable` e o principal ativo para `contact_draft_channels`.
Não pode reativar um canal desativado apenas porque a pessoa foi associada
novamente a uma lista.

O card não atualiza `contact_list_members` já materializados. A materialização
operacional é um contrato separado e explícito do `source_list_membership`: ela
associa a lista ao flow sem fan-out, confirma os membros e entrega somente o
escopo pessoa/lista. `select_contact_channel` continua responsável por escolher
e revincular o endereço na mesma sessão.

## Segurança e rollback

- Nenhuma alteração em discador, Supplier V1/V2 ou callbacks.
- Nenhuma migration.
- Nenhum fan-out ou comunicação implícita.
- Erros de configuração recebem `422` nos campos visuais do card.
- Erros runtime `manage_contact_channels_*` são terminais quando não houver
  branch `exception`, evitando retry loop.
- Rollback: remover o catálogo antes de reverter a engine; definitions já
  publicados com o card devem ser pausados ou republicados sem ele.

## Evidência local inicial

- 116 testes de engine/cards vizinhos passaram.
- 4 testes PostgreSQL de canal/lista/seletor passaram fora da sandbox.
- A prova PostgreSQL confirmou duas pessoas com o mesmo telefone, repetição
  idempotente, desativação e promoção segura da projeção legada.
- 98 testes direcionados do Target Core passaram, incluindo catálogo, 422 e
  regressões de `create_contact`, `source_list_membership` e
  `select_contact_channel`.

O canário D no workspace HighComm deve ser criado somente depois do merge e do
deploy coordenado de catálogo e engine. Depois da homologação D, a sequência
obrigatória continua sendo: portabilidade dos fluxos Velox de referência e,
por fim, retomada do fluxo completo
`c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.
