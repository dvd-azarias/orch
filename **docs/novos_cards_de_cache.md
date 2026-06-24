# Cache Cards (`cache_post` e `cache_get`)

## Objetivo
Documentar, de ponta a ponta, como os cards de cache funcionam no Target Core:

- escrita de dados com `cache_post`
- leitura de dados com `cache_get`
- relação com a tabela `cache_card_store` no schema `ws_*`
- comportamento no runtime (`/v5/runner/start` e `/v5/runner/step`)

---

## Visão geral da arquitetura

### Componentes
- **`cache_post`**: persiste (upsert) um objeto JSON em cache.
- **`cache_get`**: recupera o objeto pelo trio lógico (`flow_uuid`, `name`, `cache_key`) e expiração válida.

### Runtime
Ambos são executados pelo mesmo `FlowEngine`:

- `app/services/runner.py`  
  - handler `cache_post`
  - handler `cache_get`

Isso significa que funcionam tanto em:

- `POST /v5/runner/start`
- `POST /v5/runner/step`

---

## Estrutura no banco (`cache_card_store`)

Tabela por workspace:  
`"ws_<workspace_uuid>".cache_card_store`

Campos principais:

- `id` (PK)
- `flow_uuid` (`uuid`)
- `card_uuid` (`uuid`) → `ref_id` do card no flow
- `name` (`text`) → nome lógico do cache
- `cache_key` (`text`) → chave primária lógica da entrada
- `data` (`jsonb`) → payload armazenado
- `expires_at` (`timestamptz`) → validade da entrada
- `created_at`, `updated_at`

Regras de unicidade:

- `(flow_uuid, card_uuid, cache_key)`
- `(flow_uuid, name, cache_key)`

TTL padrão:

- `7 dias` (quando não for informado `ttl_days`)

---

## Card `cache_post` (escrita)

## Parâmetros (catálogo)

### `name` (obrigatório)
- Tipo: `text`
- Função: nome lógico do conjunto cacheado (ex.: `dados_cliente`).

### `primary_key_field` (obrigatório)
- Tipo: `text`
- Função: informa qual `key` do `mapping` será usada como chave primária lógica (`cache_key`).
- Exemplo: `identificador`.

### `mapping` (obrigatório)
- Tipo: `mapping-table`
- Função: define os campos que serão persistidos em `data` (JSON).
- Cada item contém:
  - `key` (nome do campo)
  - `value` (valor, com suporte a template)

### `ttl_days` (opcional, runtime)
- Se informado, sobrescreve o TTL padrão.
- Se ausente/inválido, usa 7 dias.

## Resolução em runtime

1. Resolve `workspace_uuid` e `flow_uuid` do estado da sessão.
2. Resolve templates de `name` e `mapping`.
3. Define `cache_key` com base no campo informado em `primary_key_field`.
4. Faz `INSERT ... ON CONFLICT` para upsert em `cache_card_store`.
5. Atualiza `expires_at` com `NOW() + ttl`.

## Persistência (resumo)

- Identidade lógica principal de leitura: `(flow_uuid, name, cache_key)`.
- Se existir mesmo `card_uuid/cache_key` com `name` diferente, o handler limpa o conflito antes do upsert.

## Branches do `cache_post`

- `proximo` (fluxo normal)
- `exception` (se houver branch configurada e ocorrer erro de validação/persistência)

Erros comuns:

- `cache_post.invalid_parameters`
- `cache_post.persist_failed`
- `cache_post.persist_exception:*`

---

## Card `cache_get` (leitura)

## Parâmetros (catálogo)

### `name` (obrigatório)
- Tipo: `text`
- Deve ser o mesmo nome usado no `cache_post`.

### `key` (obrigatório)
- Label no catálogo: **Valor de busca (na chave primária)**
- Tipo: `text`
- Deve ser o mesmo valor que foi usado como `cache_key` na escrita.

### `output_var` (opcional)
- Label no catálogo: **Variável para acomodar o dado (se encontrado)**
- Tipo: `text`
- Caminho dentro de `customs` para armazenar o JSON retornado.
- Se vazio, usa `name` como destino.

Exemplo:

- `output_var = dados_cliente`
- resultado disponível em `variables.customs.dados_cliente`

## Query de leitura (resumo)

Busca por:

- `flow_uuid`
- `name`
- `cache_key`
- `expires_at > NOW()`

Ordenação:

- `updated_at DESC, id DESC`

Limite:

- `1 registro`

## Branches do `cache_get`

- `encontrado`
- `nao_encontrado`
- `exception`

### Regras atuais de roteamento

Quando o `name` do `cache_get` **não existe** em nenhum `cache_post` do flow:

- retorna erro `cache_get.cache_name_not_configured`
- segue para `exception`

Quando o `name` existe, mas **não há registro** para a chave consultada:

- segue para `nao_encontrado` (sem erro técnico)
- se não houver `nao_encontrado` ligado, usa fallback para `exception` com `cache_get.not_found`

Quando encontra:

- grava o payload em `variables.customs.<output_var>`
- segue para `encontrado`

Quando há falha técnica de acesso:

- erro `cache_get.fetch_failed` (ou erro retornado pelo banco)
- segue para `exception`

---

## Como configurar no fluxo (padrão recomendado)

## Escrita
`cache_post`

- `name`: `dados_cliente`
- `primary_key_field`: `identificador`
- `mapping`:
  - `identificador` → `{{identificador_cliente}}`  ← chave primária lógica
  - `nome` → `{{nome_cliente}}`

## Leitura
`cache_get`

- `name`: `dados_cliente`
- `key`: `{{identificador_cliente}}`  ← deve bater com a escrita
- `output_var`: `dados_cliente`

## Uso posterior

- `{{customs.dados_cliente.identificador}}`
- `{{customs.dados_cliente.nome}}`

---

## Erros de configuração mais comuns

1. **`name` no get não existe em nenhum `cache_post` do flow**
   - Resultado: `cache_get.cache_name_not_configured` + `exception`.

2. **`key` no get diferente da chave usada no post**
   - Resultado: `nao_encontrado`.
   - Ex.: escreve com `identificador_cliente` e lê com `nome_cliente`.

3. **`primary_key_field` ausente ou inválido no post**
   - Pode gerar `cache_post.invalid_parameters`.

4. **Sem branch `exception` no `cache_get`**
   - Erros técnicos continuam sem destino explícito no fluxo.

---

## Observações de manutenção

- O card antigo `cache` foi evoluído para **`cache_post`**.
- O card de leitura é **`cache_get`**.
- O catálogo em `app/commands/catalog.py` já expõe ambos explicitamente.
- A chave primária lógica do cache é definida explicitamente em `primary_key_field`.
