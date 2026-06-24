# GAP no `api_call` (runtime M2): `body.mode` incompatível com catálogo/UI

## Resumo executivo

Identificamos um GAP no componente `api_call` do runtime M2: alguns valores de `body.mode` gerados/configurados no catálogo/UI não eram reconhecidos pelo executor em `workflow_m2_service`.

Na prática, chamadas HTTP com `mode = "raw"` e `mode = "x-www-form-urlencoded"` podiam ser enviadas **sem body**, mesmo com fluxo seguindo por branch de sucesso quando o endpoint retornava `200`.

---

## Contexto observado

- Workspace analisado: `ws_07defb71-4546-4250-885c-e96abb023a78`
- Flow analisado: `29520561-0367-410b-9795-5af546e75e96`
- Revisão publicada observada na investigação: `v60` (em `2026-06-23`)

No `definition` do fluxo havia cards `api_call` com:

- `body.mode = "x-www-form-urlencoded"`
- `body.mode = "raw"`

---

## Sintoma funcional

1. O fluxo executava `api_call`.
2. `api_call_last_result.status_code` retornava `200` em várias execuções.
3. Mesmo assim, payload esperado não chegava corretamente no destino (ou chegava vazio), dando percepção de “POST não funcionando certinho”.

---

## Causa raiz técnica

No runtime M2 (`app/services/workflow_m2_service.py`), a função `_resolve_body(...)` tratava apenas:

- `json`
- `text`
- `form`

Modos usados no catálogo/UI como:

- `x-www-form-urlencoded`
- `raw`

não tinham tratamento explícito, resultando em retorno `(None, None)` para body/content-type nesses casos.

---

## Impacto

- Requisições com esses modos podem sair sem body.
- Endpoint pode retornar `200` mesmo com body inválido/vazio.
- Fluxo segue branch `success`, mascarando erro semântico da integração.
- Diagnóstico fica difícil porque o status HTTP aparenta sucesso.

---

## Correção aplicada (referência)

Arquivo: `app/services/workflow_m2_service.py`

1. `x-www-form-urlencoded` passou a ser tratado como alias de `form`.
2. `raw` passou a montar body com interpolação de template:
   - usa `body.json` preferencialmente;
   - fallback para `body.text`;
   - define content-type apropriado.

Também foi mantido suporte aos modos já existentes (`json`, `text`, `form`, `none`/fallback).

---

## Testes de regressão adicionados

Arquivo: `tests/test_workflow_m2_service.py`

- `test_resolve_body_supports_x_www_form_urlencoded_alias`
- `test_resolve_body_supports_raw_mode_with_template_interpolation`

Resultado local (módulo): `76 passed`.

---

## Como validar em outra aplicação

### 1) Inspecionar definição do fluxo
Verifique se os `api_call` usam `body.mode` em:

- `raw`
- `x-www-form-urlencoded`

### 2) Conferir runtime atual
No executor HTTP, confirme se esses modos são realmente tratados (não apenas `json/text/form`).

### 3) Reproduzir rapidamente
Monte um `api_call` com:

- URL de webhook de inspeção
- `body.mode = raw` com template (ex.: `{"campo":"{{variavel}}"}`)
- `body.mode = x-www-form-urlencoded` com 1-2 pares chave/valor

Valide no destino se o body chegou com conteúdo esperado.

### 4) Garantir teste automatizado
Adicione teste de unidade para função que resolve body HTTP garantindo:

- bytes não nulos para `raw`
- `Content-Type: application/x-www-form-urlencoded` para `x-www-form-urlencoded`

---

## Recomendação de padronização

Para evitar reincidência:

1. Definir contrato único de `body.mode` entre catálogo/UI e runtime.
2. Tratar aliases conhecidos no backend (`form`, `x-www-form-urlencoded`, `urlencoded`).
3. Em runtime, logar warning quando `mode` não reconhecido.
4. Opcional: falhar explicitamente (erro técnico) quando `mode` inválido, para não mascarar execução.

---

## TL;DR para repasse

O GAP era de compatibilidade de `body.mode` no `api_call`: fluxo usava `raw` e `x-www-form-urlencoded`, runtime só entendia `json/text/form`, então algumas requisições iam sem body apesar de status 200.  
Ajuste necessário: suportar esses modos/aliases no resolvedor de body + testes de regressão.

