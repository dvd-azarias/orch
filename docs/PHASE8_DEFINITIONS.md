# Fase 7 — Organização dos Mecanismos de Carga de Arquivos em ORCH

## 1. Objetivo da fase

Esta fase tem como objetivo organizar e implementar a decisão de comportamento do evento `FileApp` dentro do ORCH, separando claramente os dois propósitos possíveis de um arquivo carregado na plataforma.

Um arquivo carregado pode ter dois usos objetivos:

| Tipo | Finalidade | Destino principal |
|---|---|---|
| `tipo_1` | Carga de `persons`, que representam contatos | Tabela `ws_*.persons` |
| `tipo_2` | Carga simples de arquivo para consumo por flows do tipo orchestration | Tabela `orch_sessions` |

A decisão entre `tipo_1` e `tipo_2` deverá ser feita a partir da existência ou não de `mapping_template` configurado no evento `FileApp`.

---

## 2. Contexto geral

Atualmente existem dois caminhos conceituais para carga de arquivos na plataforma:

1. **Carga via Tela / Frontend**
2. **Carga via acomodação na Arquivos APP**

A Fase 7 trata especificamente do segundo caminho, mas deve reutilizar o mecanismo já existente no primeiro caminho quando o arquivo for do `tipo_1`.

A premissa principal é manter **um único mecanismo lógico de importação de contatos para `ws_*.persons`**, evitando duplicidade de regras de negócio.

---

# 3. Cenário 01 — Carga via Tela / Frontend

## 3.1 Finalidade

Este cenário é **exclusivo para arquivos do `tipo_1`**.

Ou seja, ele atende somente cargas cujo objetivo seja importar dados para `ws_*.persons`, onde `persons` representa os contatos da aplicação.

## 3.2 Estado atual

Este mecanismo **já existe e já funciona** em uma aplicação terceira, tanto no backend quanto no frontend.

Portanto, nesta fase:

- Não devemos modificar este mecanismo.
- Não devemos refatorar este fluxo.
- Não devemos recriar a lógica de importação de `persons` dentro do ORCH.
- Devemos apenas reutilizar suas APIs quando necessário, especialmente no `Cenário 02 / tipo_1`.

## 3.3 Workflow lógico já existente

A carga via tela ocorre por meio da Target-Core API e é composta por três etapas macro obrigatórias.

### Etapa A — Upload do arquivo

Momento em que o arquivo cru entra na plataforma.

Nesta etapa, o arquivo ainda não foi interpretado como contatos nem teve suas colunas associadas aos campos da tabela `ws_*.persons`.

### Etapa B — Mapeamento de campos

Momento em que as colunas do arquivo tabulado são mapeadas contra os campos da tabela `ws_*.persons`.

Em outras palavras, esta etapa declara como cada coluna do arquivo será interpretada como campo de `persons`.

Exemplo conceitual:

```text
coluna do arquivo: nome_cliente  -> campo em ws_*.persons: name
coluna do arquivo: cpf           -> campo em ws_*.persons: identifier
coluna do arquivo: telefone      -> campo em ws_*.persons: phone
```

### Etapa C — Importação para o banco de dados

Momento em que os dados do arquivo são efetivamente copiados, inseridos ou atualizados na tabela `ws_*.persons`.

Esta é a etapa que materializa a carga de contatos no banco de dados.

## 3.4 Observação sobre assign/unassign

Existe ainda um mecanismo complementar de `assign` e `unassign`, responsável por vincular ou desvincular uma `source_list` / mailing a um flow.

Este mecanismo também já existe e já manipula a tabela `orch_sessions`.

Comportamento conhecido:

- No `assign`, registros são inseridos na tabela `orch_sessions`.
- No `unassign`, registros são removidos da tabela `orch_sessions`.

Este mecanismo é citado apenas para conhecimento e contexto.

Nesta Fase 7, não devemos alterar este comportamento, salvo se for estritamente necessário para manter compatibilidade com a nova lógica do evento `FileApp`.

---

# 4. Cenário 02 — Carga via acomodação na Arquivos APP

## 4.1 Finalidade

Este é o cenário que será implementado ou organizado nesta Fase 7.

A carga via Arquivos APP poderá representar dois tipos de uso:

- `tipo_1`: carga de contatos em `ws_*.persons`.
- `tipo_2`: carga simples de arquivo para consumo direto por flows de orchestration.

## 4.2 Ponto central da implementação

Quando o ORCH receber um evento do tipo `FileApp`, ele deverá decidir imediatamente qual caminho seguir.

A decisão será baseada na existência ou não de `mapping_template` configurado.

Regra de decisão:

| Condição no evento `FileApp` | Tipo assumido | Comportamento esperado |
|---|---|---|
| Com `mapping_template` configurado | `tipo_1` | Disparar sequência de APIs da Target-Core API para carga em `source_list` / `persons` |
| Sem `mapping_template` configurado | `tipo_2` | Seguir o comportamento atual de leitura do arquivo e inserção linha a linha em `orch_sessions` |

---

# 5. Comportamento esperado para `tipo_1`

## 5.1 Definição

Um arquivo será considerado `tipo_1` quando o evento `FileApp` possuir `mapping_template` configurado.

Neste caso, o arquivo representa uma carga de contatos/persons.

## 5.2 Comportamento esperado

Quando o arquivo for identificado como `tipo_1`, o ORCH deverá reutilizar as APIs já existentes do fluxo de carga via tela.

O objetivo é percorrer o mesmo caminho lógico já existente no `Cenário 01`.

Sequência conceitual esperada:

1. Receber o evento `FileApp`.
2. Identificar que existe `mapping_template` configurado.
3. Classificar o evento como carga `tipo_1`.
4. Acionar a sequência de APIs já documentadas da Target-Core API.
5. Fazer com que o arquivo seja tratado como uma carga de `source_list` / mailing.
6. Permitir que o fluxo existente realize:
   - upload lógico do arquivo;
   - aplicação/mapeamento dos campos conforme o template;
   - importação para `ws_*.persons`.
7. Não inserir diretamente linhas em `orch_sessions` neste caminho.

## 5.3 Regra importante

Para `tipo_1`, o ORCH **não deve manipular diretamente a tabela `orch_sessions`** durante o tratamento do evento `FileApp`.

A responsabilidade do ORCH neste caso é orquestrar a chamada das APIs já existentes, mantendo a lógica centralizada no mecanismo já consolidado de carga de contatos.

## 5.4 O que precisa ser implementado

Este mecanismo ainda não existe dentro da realidade do `Cenário 02`.

Portanto, precisa ser implementada a lógica que:

- detecta `mapping_template` no evento `FileApp`;
- classifica o evento como `tipo_1`;
- dispara a sequência correta de APIs já documentadas;
- garante que o comportamento seja equivalente ao fluxo de carga via tela;
- evita qualquer inserção direta em `orch_sessions` por este caminho.

---

# 6. Comportamento esperado para `tipo_2`

## 6.1 Definição

Um arquivo será considerado `tipo_2` quando o evento `FileApp` **não possuir `mapping_template` configurado**.

Neste caso, o arquivo não representa uma carga formal de contatos/persons.

Ele representa apenas um arquivo tabulado a ser consumido por flows do tipo orchestration.

## 6.2 Comportamento esperado

Quando o arquivo for identificado como `tipo_2`, o ORCH deverá seguir o comportamento atual.

Ou seja:

1. Receber o evento `FileApp`.
2. Identificar que não existe `mapping_template` configurado.
3. Classificar o evento como carga `tipo_2`.
4. Abrir o arquivo.
5. Ler o conteúdo linha a linha.
6. Inserir os registros correspondentes na tabela `orch_sessions`.

## 6.3 Estado atual

Este mecanismo já existe hoje, pois o evento `FileApp` já realiza inserções em `orch_sessions`.

Nesta fase, o objetivo não é recriar esta lógica, mas sim revisá-la para garantir que ela conviva corretamente com a nova decisão entre `tipo_1` e `tipo_2`.

## 6.4 Regra importante

A inserção em `orch_sessions` só deve acontecer quando o evento `FileApp` for classificado como `tipo_2`.

Se houver `mapping_template` configurado, o fluxo deve ser `tipo_1` e, portanto, a inserção direta em `orch_sessions` deve ser evitada neste ponto.

---

# 7. Regra central da Fase 7

A Fase 7 consiste em trabalhar o evento `FileApp` para decidir entre dois comportamentos:

```text
Evento FileApp recebido
        |
        v
Existe mapping_template configurado?
        |
        |-- Sim --> tipo_1 --> usar APIs da Target-Core API para carga de source_list/persons
        |
        |-- Não --> tipo_2 --> manter comportamento atual de leitura do arquivo e inserção em orch_sessions
```

A regra principal é:

- Com `mapping_template`: tratar como carga de contatos/persons, usando o mecanismo já existente da Target-Core API.
- Sem `mapping_template`: tratar como arquivo simples de orchestration, mantendo o comportamento atual de popular `orch_sessions`.

---

# 8. Escopo da implementação

## 8.1 Deve ser feito

- Ajustar o tratamento do evento `FileApp`.
- Criar a decisão explícita entre `tipo_1` e `tipo_2`.
- Usar a presença de `mapping_template` como critério objetivo de decisão.
- Para `tipo_1`, invocar a sequência de APIs já documentadas da Target-Core API.
- Para `tipo_2`, preservar o comportamento atual de inserção em `orch_sessions`.
- Garantir que o caminho `tipo_1` não faça inserção direta em `orch_sessions`.
- Garantir que o caminho `tipo_2` não tente disparar o workflow de carga de `persons`.
- Manter a lógica de carga de contatos centralizada no mecanismo já existente.

## 8.2 Não deve ser feito

- Não alterar o mecanismo já existente de carga via tela.
- Não recriar dentro do ORCH a lógica de importação para `ws_*.persons`.
- Não duplicar regras de mapeamento de campos.
- Não alterar desnecessariamente o mecanismo de `assign` / `unassign`.
- Não inserir em `orch_sessions` quando o evento possuir `mapping_template`.
- Não tratar todo arquivo `FileApp` como orchestration simples sem antes avaliar `mapping_template`.

---

# 9. Critérios de aceite

A implementação será considerada correta quando os seguintes critérios forem atendidos:

## 9.1 Para evento `FileApp` com `mapping_template`

- O evento é classificado como `tipo_1`.
- O ORCH dispara a sequência de APIs da Target-Core API.
- O arquivo segue o mesmo caminho lógico da carga via tela.
- Os dados são importados para `ws_*.persons` por meio do mecanismo já existente.
- Nenhuma linha é criada diretamente em `orch_sessions` durante este tratamento.

## 9.2 Para evento `FileApp` sem `mapping_template`

- O evento é classificado como `tipo_2`.
- O comportamento atual é preservado.
- O arquivo é aberto e processado linha a linha.
- Os registros são inseridos em `orch_sessions`.
- Nenhuma chamada indevida ao workflow de carga de `persons` é realizada.

## 9.3 Para compatibilidade geral

- O comportamento existente de `assign` / `unassign` permanece funcional.
- O fluxo já existente de carga via tela permanece inalterado.
- A regra de decisão fica clara, explícita e fácil de manter.
- Logs devem permitir identificar se o evento foi tratado como `tipo_1` ou `tipo_2`.

---

# 10. Observação final para implementação

A Fase 7 não deve criar um novo mecanismo de importação de contatos.

A decisão arquitetural correta é reutilizar a cadeia de APIs já existente para carga de `persons`, garantindo que tanto a carga via tela quanto a carga via Arquivos APP, quando forem `tipo_1`, passem pelo mesmo mecanismo.

Portanto, o foco técnico desta fase é:

1. Detectar corretamente a presença de `mapping_template` no evento `FileApp`.
2. Separar o comportamento entre `tipo_1` e `tipo_2`.
3. Preservar o comportamento atual para `tipo_2`.
4. Implementar o caminho novo para `tipo_1` por meio da chamada das APIs já existentes.
5. Evitar duplicidade de regra de negócio e manter a carga de contatos centralizada.
