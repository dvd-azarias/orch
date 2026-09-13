# Plano postergado — Supplier V2 com Dial Rule configurada

## Estado

- **Status:** `POSTERGADO`, não cancelado.
- **Registrado em:** 2026-09-11.
- **Classificação proposta quando retomado:** `ALPHA_FIX_REQUIRED`.
- **Motivo do adiamento:** a diretoria priorizou a criação de um CRUD de Dial Rules na Supplier. As regras deixarão de ser definidas dentro do card; o card manterá somente uma combo para selecionar uma Dial Rule criada e administrada por esse CRUD.
- **Condição para retomada:** concluir o seletor `dial_profile_id` no catálogo do
  novo card, adaptar este plano ao snapshot publicado do Perfil e confirmar o
  contrato de decisão da Supplier V2.
- **Checkpoint de 2026-09-12:** migration e CRUD foram mergeados pelas PRs
  Target Core `#483`/`#484`; UI e BFF reais estão ativos no `.239`, restritos ao
  workspace aprovado e acessíveis somente por túnel SSH. O canário de escrita e
  arquivamento passou. Este plano continua postergado apenas até o catálogo
  selecionar o Perfil e o desenho abaixo ser reconciliado com esse snapshot.

Este documento preserva integralmente o plano aprovado conceitualmente antes da mudança de prioridade. Nenhum item abaixo deve ser interpretado como já implementado. Quando o trabalho for retomado, o desenho da Dial Rule deverá ser reconciliado com o novo CRUD; as fronteiras de segurança entre Supplier V1, Supplier V2 e ORCH devem ser preservadas.

## Decisões já consolidadas

1. O card legado `send_with_dialer` e o Supplier V1 não podem sofrer alteração comportamental.
2. O novo card `send_with_dialer_handoff` utilizará um caminho Supplier V2 isolado.
3. As rotas novas devem usar o prefixo `/v2/contact-supplier`.
4. A Dial Rule usada pelo Supplier V2 será a selecionada no card, nunca `DEFAULT_DIALRULE`.
5. A seleção deve vir da revisão do flow fixada na sessão, e a regra deve ser materializada como snapshot imutável para o ciclo em andamento.
6. O Supplier é responsável por seleção, releases, contadores, intervalos e retentativa.
7. O ORCH é responsável pelo `linked_actuator`, pela sessão e pela navegação do canvas.
8. O ORCH somente percorre um branch quando o Supplier declarar uma decisão terminal.
9. Decisões intermediárias devem ser registradas para auditoria, mas não podem avançar o canvas.
10. Não pode existir fallback silencioso do Supplier V2 para a regra default ou para o comportamento legado.

# Plano final preservado

## 1. Garantias de escopo

Não alterar:

- `send_with_dialer` legado;
- `/v1/contact-supplier`;
- `sbc_feedback.process`;
- `apply_feedback()` atual;
- `DEFAULT_DIALRULE`;
- callbacks e dados dos clientes atuais.

Somente `send_with_dialer_handoff` utilizará o Supplier V2.

A classificação proposta é `ALPHA_FIX_REQUIRED`, devido à divergência observada entre o controle de retentativa e a navegação do canvas.

## 2. Semântica definitiva da Dial Rule

O desenho conceitual discutido para cada release foi:

```json
{
  "max_attempts": 2,
  "retry_target": "same_address",
  "on_exhausted": "done",
  "interval": 300
}
```

Regras:

- `max_attempts`: quantidade máxima, incluindo a primeira ocorrência;
- `same_address`: tenta novamente o mesmo telefone;
- `next_address`: utiliza o próximo telefone conforme `label_order`;
- `done`: encerra normalmente, sem atendimento;
- `abort`: cancela definitivamente o ciclo;
- `interval`: data mínima da próxima tentativa;
- `answered`: sempre terminal e ignora qualquer retentativa.

A `renitencia` existente é armazenada em JSONB; portanto, uma ampliação semântica desse documento não exige, por si só, alteração da tabela `dial_rules`.

Os registros antigos não devem ser reescritos automaticamente. Para o canário, deve ser criada ou ajustada explicitamente uma regra dedicada ao Supplier V2.

### Adaptação obrigatória ao retomar

O novo CRUD solicitado pela diretoria passa a ser a fonte da verdade da Dial Rule. Antes de implementar esta seção, revisar:

- o envelope persistido pelo CRUD;
- a semântica definitiva de tentativas e retentativas;
- como `same_address`, `next_address`, `done` e `abort` serão representados;
- validações de criação e atualização;
- tratamento de regras antigas;
- regras em uso que não podem ser excluídas ou modificadas de maneira destrutiva;
- resposta da API usada pela combo do card.

O card não deve voltar a incorporar a configuração detalhada da regra. Ele deve persistir somente a seleção necessária, preferencialmente `dial_rule_id`.

## 3. Persistência própria do Supplier V2

Para garantir pin de revisão, idempotência e isolamento, o Target Core/Supplier deverá possuir estruturas aditivas, por exemplo:

- `contact_supplier_dial_cycles_v2`;
- `contact_supplier_dial_events_v2`.

O ciclo deverá armazenar:

- sessão do ORCH;
- workspace;
- fluxo;
- revisão fixada;
- card/ref;
- lista e membro;
- `dial_rule_id`;
- snapshot imutável da Dial Rule;
- estado do ciclo;
- resultado terminal;
- token opaco de callback.

Os eventos deverão armazenar:

- ciclo;
- `DialerActionID` ou `Uniqueid`;
- payload ou metadados estritamente necessários;
- release normalizada;
- decisão;
- data de processamento.

Uma chave única deverá impedir que um callback duplicado incremente os contadores duas vezes.

Antes do código, verificar se já existe persistência adequada. Se ela não existir, produzir uma especificação técnica para a equipe responsável criar somente a migration, seguindo o playbook do Target Core.

## 4. Rotas exclusivas do Supplier V2

Criar um router separado:

```text
/v1/contact-supplier → legado
/v2/contact-supplier → novo contrato
```

Rotas conceitualmente previstas:

```text
POST /v2/contact-supplier/dialer-cycles
POST /v2/contact-supplier/{contact_list_id}/initialize
GET  /v2/contact-supplier/{contact_list_id}/select
POST /v2/contact-supplier/dialer-feedback/{callback_token}
GET  /v2/contact-supplier/dialer-cycles/{cycle_id}
```

Os caminhos e envelopes finais devem ser reconciliados com o novo CRUD de Dial Rules antes da implementação.

Princípios:

- V2 nunca chama `apply_feedback()` legado;
- V2 nunca utiliza `DEFAULT_DIALRULE`;
- V2 nunca faz fallback silencioso para V1;
- ciclo ausente, regra inválida ou workspace divergente gera erro explícito;
- a rota de feedback responde apenas `202/queued`.

## 5. Registro do ciclo pelo ORCH

Quando alcançar `send_with_dialer_handoff`, o ORCH deverá:

1. Obter o `dial_rule_id` selecionado no card da revisão fixada da sessão.
2. Manter sua responsabilidade de marcar `linked_actuator=dialer`.
3. Materializar `list_validity`.
4. Solicitar ao Target Core/Supplier a criação do ciclo V2.
5. Permitir que o Target valide a regra dentro do mesmo workspace por meio do novo CRUD.
6. Permitir que o Target crie o snapshot da regra.
7. Gravar o identificador do ciclo no runtime.
8. Manter a sessão bloqueada no card.

Se o registro do ciclo falhar, a sessão não deve ser liberada para uma discagem incompleta.

## 6. Seleção e feedback no Target Core/Supplier

A inicialização e a seleção V2 deverão utilizar exclusivamente o snapshot:

- `label_order`;
- limites;
- intervalos;
- estratégia de próximo endereço;
- ação ao esgotar;
- `list_validity`.

Quando chegar um CDR:

1. Validar token e ciclo.
2. Aplicar idempotência.
3. Normalizar a release.
4. Incrementar o contador correto.
5. Atualizar `contact_list_members`.
6. Decidir entre nova tentativa ou resultado terminal.
7. Registrar a decisão.
8. Notificar o ORCH.

A normalização deverá ser única no Supplier V2, incluindo os casos observados:

```text
490 + kcpa_Silencio → machine
16 + kcpa_Human     → answered
```

## 7. Contrato Target Core/Supplier → ORCH

Evento intermediário:

```json
{
  "event_id": "...",
  "cycle_id": "...",
  "session_uuid": "...",
  "terminal": false,
  "outcome": "machine",
  "action": "retry_same_address",
  "attempt": 1,
  "limit": 2,
  "retry_at": "..."
}
```

Evento terminal:

```json
{
  "event_id": "...",
  "cycle_id": "...",
  "session_uuid": "...",
  "terminal": true,
  "outcome": "answered",
  "attempt": 2,
  "limit": 2
}
```

No ORCH:

- `terminal=false`: registra a tentativa e continua bloqueado;
- `terminal=true`: retoma exatamente a sessão e o card originais;
- o branch é determinado por `outcome`;
- eventos duplicados são reconhecidos sem nova execução;
- eventos de outro ciclo, revisão ou sessão são recusados.

A “decisão” acima é o resultado do processamento assíncrono da Supplier, e não a resposta síncrona entregue ao Asterisk. A rota de entrada confirma somente o enfileiramento.

## 8. Fila e observabilidade

Criar para o Supplier V2:

- task Celery própria;
- fila própria;
- hostname explícito nos testes;
- métricas separadas;
- logs com `supplier_version=v2`.

Logs mínimos:

- ciclo;
- sessão;
- membro;
- regra e revisão;
- release bruta e normalizada;
- contador e limite;
- decisão;
- terminalidade;
- motivo de descarte ou idempotência.

## 9. Validações e `422`

Proteger:

- card sem `dial_rule_id`;
- regra removida ou pertencente a outro workspace;
- revisão ausente;
- `max_attempts < 1`;
- `interval < 1`;
- `retry_target` inválido;
- `on_exhausted` inválido;
- `next_address` sem `label_order`;
- ciclo incompatível com lista, membro ou sessão.

As respostas devem seguir o padrão de erro por campo usado pelo Target Core.

As validações específicas do CRUD de Dial Rules deverão impedir que uma regra inconsistente fique disponível na combo do card.

## 10. Testes

Testes automatizados:

- mesmo endereço;
- próximo endereço;
- `done`;
- `abort`;
- ausência de outro telefone;
- intervalos;
- `answered` terminal;
- callback duplicado;
- callbacks fora de ordem;
- regra alterada durante o ciclo;
- regra de outro workspace;
- `list_validity`;
- mapeamento `490/kcpa_Silencio`;
- regressão completa do Supplier V1.

E2E real:

1. Uma pessoa e um telefone.
2. Primeira chamada resulta em `machine`.
3. Supplier V2 produz `terminal=false`.
4. A sessão permanece bloqueada.
5. Segunda chamada é atendida.
6. Supplier V2 produz `terminal=true/answered`.
7. ORCH percorre `answered`.
8. O fluxo chega ao `Aguardar Evento`.

Depois, validar:

- uma pessoa com vários telefones;
- `next_address`;
- `busy` até o limite;
- `done`;
- `abort`.

## 11. Branches e implantação

Branches independentes, ambas partindo de `main` atualizado:

```text
target-core: feat/contact-supplier-v2-dial-rule
orch:        feat/dialer-handoff-supplier-v2
```

Ordem proposta:

1. Concluir o novo CRUD de Dial Rules e seu contrato.
2. Adaptar este plano ao CRUD homologado.
3. Criar a migration aditiva necessária para ciclos e eventos, caso confirmada.
4. Implementar e testar o Target Core/Supplier V2.
5. Implantar o Target Core com as rotas ainda sem consumidor.
6. Integrar o ORCH ao ciclo V2.
7. Implantar o ORCH.
8. Ativar apenas no fluxo de teste.
9. Executar discagem real controlada.
10. Validar estados, retentativas e branches.
11. Retomar o fluxo completo.

## 12. Rollback

- impedir a criação de novos ciclos V2;
- preservar ciclos e eventos existentes para auditoria;
- reverter somente o novo card ou caminho V2;
- manter V1 e o discador legado funcionando sem intervenção;
- não apagar nem reinterpretar dados históricos automaticamente.

## Próxima atividade, antes da retomada deste plano

Levantar com o responsável do produto os requisitos do novo CRUD de Dial Rules, incluindo:

- campos e tipos;
- releases suportadas;
- semântica de tentativas;
- ações e intervalos;
- ordenação de labels/canais;
- estados da regra;
- criação, listagem, consulta, edição, exclusão e restauração;
- proteção de regras em uso;
- filtros, paginação e busca;
- contrato da combo do card;
- escopo por workspace;
- auditoria e autorização;
- validações e padrão de `422`;
- estratégia de compatibilidade com o cadastro existente.

Somente depois desses requisitos este plano deve ser reaberto e adaptado.
