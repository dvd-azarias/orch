# Plano de implementação — Gestão de Discagem V2

## Estado

- **Status:** migration e CRUD real mergeados no Target Core pelas PRs `#483` e
  `#484`; CRUD implantado no `.239`; UI/BFF operacionais e o pipeline CSV de
  Listas de Restrição homologados contra o workspace aprovado. O card de
  consulta está implementado e validado localmente no catálogo e no ORCH,
  aguardando branches/PRs e o E2E pelos dois branches no canvas.
- **Data do checkpoint:** 2026-09-13.
- **Classificação:** `ALPHA_FIX_REQUIRED`, com implementação aditiva e isolada.
- **Consolidação da UI concluída:** a fonte da release ativa foi importada no
  repositório privado `GOHP-LAB/target-extensions-ui`, baseline `3035362`, com
  CI Node 22 e `main` protegida. A UI faz parte da operação e não é tratada como
  artefato descartável.
- **Objetivo operacional seguinte:** homologar integralmente o novo card no flow
  `4e163399-e9a0-4335-895f-316c6a161299` e então retomar o fluxo geral
  `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152`.
- **Objetivo posterior:** adaptar o plano de ciclos/feedback da Supplier V2 ao
  `dial_profile_id` e ao snapshot publicado do Perfil.

Este documento não autoriza migration, deploy ou alteração do Supplier V1. Cada fase que muda runtime ou infraestrutura requer autorização explícita e validação proporcional ao risco.

## Evidências confirmadas na auditoria

1. O protótipo Docker local possui CRUDs navegáveis, wizard atômico, simulador e visão 360º aprovados visualmente pelo usuário.
2. O Target Core já possui `/v1/dial-rules` e a tabela histórica `dial_rules`, com contrato baseado em `label_order` e `renitencia`.
3. O Contact Supplier V1 ainda usa `DEFAULT_DIALRULE` nos caminhos atuais de inicialização e feedback.
4. A aplicação separa routers por perfis `CRUD` e `SUPPLIER`; o router legado `contact_supplier` pertence ao perfil Supplier.
5. A trilha canônica de workspace é `migrations_target_ws`; a revision
   `20260912_0001_dialing_mgmt` sucede `20260911_0001_list_validity` e é o head
   validado no branch da migration.
6. O contrato de erros `422` já possui o envelope `message`, `errors`, `ui_message`, `error_code` e `meta`.
7. O formato comum de resposta do Target Core usa `data` e, para coleções, `extra` e `meta` de paginação.
8. O merge `fa04a26` está implantado no `.239`; o CRUD real respondeu `200` e
   retornou os Perfis do workspace `ba7eb0ec-e565-447c-8c11-8f870cf72a60`.
9. O host `.239` não possui Docker nem reverse proxy. Para não instalar um
   daemon que alteraria rede/iptables no servidor Alpha, a aplicação usa duas units
   systemd isoladas e um runtime Node 22 local ao próprio diretório.
10. O canário pelo BFF criou uma Política com `201` e a arquivou com `200`,
    passando de revisão `1` para `2`, `active=false` e ator
    `dialing-management-demo` auditado no journal.

Antes de criar a migration ou qualquer branch, atualizar o `main` local e recalcular o head; os identificadores acima são fotografias da auditoria, não valores a serem copiados cegamente.

## Fronteiras obrigatórias

Não alterar nesta fase:

- `/v1/dial-rules`;
- a tabela existente `dial_rules`;
- `/v1/contact-supplier`;
- `DEFAULT_DIALRULE`;
- `apply_feedback()` e `sbc_feedback.process` legados;
- o card `send_with_dialer`;
- flows atualmente publicados;
- os serviços e `.env` atuais do Target Core no host `.239`.

Criar de forma aditiva:

- router de configuração `dialing_management_v2`, pertencente ao perfil `CRUD`;
- namespace HTTP `/v2/contact-supplier` para os recursos novos;
- tabelas com prefixo `dialing_`, sem reaproveitar a tabela legada;
- UI operacional em duas units systemd próprias no `.239`, usando o runtime Node
  isolado já homologado;
- integração futura do novo card somente depois da homologação do CRUD.

## Contrato HTTP a congelar

### Segurança e escopo

Todas as rotas reais devem exigir:

- `Authorization: Bearer <token>`;
- `X-WORKSPACE-UUID: <uuid>`;
- validação de workspace feita pelos mecanismos existentes do Target Core;
- `Content-Type: application/json` nas mutações.

Nenhuma rota recebe nome de schema. O schema continua sendo derivado e normalizado a partir do workspace.

### Recursos

```text
GET|POST  /v2/contact-supplier/dial-rules
GET|PATCH /v2/contact-supplier/dial-rules/{id}

GET|POST  /v2/contact-supplier/dialing-calendars
GET|PATCH /v2/contact-supplier/dialing-calendars/{id}

GET|POST  /v2/contact-supplier/attempt-policies
GET|PATCH /v2/contact-supplier/attempt-policies/{id}

GET|POST  /v2/contact-supplier/spin-budgets
GET|PATCH /v2/contact-supplier/spin-budgets/{id}

GET|POST  /v2/contact-supplier/dial-profiles
GET|PATCH /v2/contact-supplier/dial-profiles/{id}

POST /v2/contact-supplier/{resource}/{id}/publish
POST /v2/contact-supplier/{resource}/{id}/archive
POST /v2/contact-supplier/{resource}/{id}/restore
POST /v2/contact-supplier/{resource}/{id}/clone
GET  /v2/contact-supplier/{resource}/{id}/usage

POST /v2/contact-supplier/guided-configurations
POST /v2/contact-supplier/simulate
```

### Convenções adicionais

- Coleções usam `page`, `per_page`, `status`, `active` e `search` quando aplicável.
- Respostas devem usar `format_response`; a UI será adaptada ao envelope real, e não o contrário.
- `PATCH`, publish, archive e restore devem exigir revisão esperada (`expected_revision` ou `If-Match`) para impedir perda silenciosa de atualização concorrente.
- Conflitos de revisão retornam `409` com código estável; inconsistências de domínio retornam `422` por campo.
- Recursos nunca são apagados fisicamente pela API.
- Archive de recurso ainda referenciado por Perfil não arquivado deve ser recusado com `422` e lista de usos.
- O card lista apenas Perfis `published` e `active=true`.
- Uma campanha (`flow_uuid`) pode ter somente um Perfil efetivo dentro do workspace.

## Semântica de revisão e publicação

1. Editar altera o rascunho e incrementa `revision`.
2. Publicar cria snapshot imutável e atualiza `published_revision_id` na mesma transação.
3. Publicar um Perfil resolve e inclui no snapshot:
   - o próprio Perfil;
   - mapeamentos geográficos e fallback;
   - Regra e respectiva Política de Tentativas;
   - Calendário e exceções;
   - Orçamento e campanhas.
4. Alterar um recurso compartilhado não muda retroativamente o Perfil já publicado.
5. Para uma alteração compartilhada entrar em vigor, o recurso e os Perfis afetados precisam ser republicados conscientemente.
6. O runtime futuro fixa o `dial_profile_revision_id` e trabalha com o snapshot do agregado, nunca com joins mutáveis durante o ciclo.
7. O wizard publica todos os recursos e o Perfil em uma única transação; qualquer falha deve deixar zero recursos parciais.

## Persistência proposta

Todas as tabelas pertencem ao schema `ws_<workspace_uuid>` e devem usar tablespace do workspace conforme o playbook vigente.

### Colunas comuns dos recursos

Cada tabela principal deve possuir, no mínimo:

```text
id uuid PK
name varchar(120)
description varchar(500) nullable
status varchar(16) CHECK draft|published|archived
active boolean
revision integer >= 1
published_revision_id uuid nullable
created_at timestamptz
updated_at timestamptz
created_by varchar nullable
updated_by varchar nullable
archived_at timestamptz nullable
archived_by varchar nullable
```

Nomes são únicos por tipo enquanto o recurso não estiver arquivado.

### Tabelas de configuração

#### `dialing_attempt_policies`

- colunas comuns;
- `outcomes jsonb NOT NULL`;
- Pydantic valida exatamente um item por evento aceito, máximo, intervalo, consumo de budget e ação terminal.

#### `dialing_rules_v2`

- colunas comuns;
- `max_spins_per_person integer NOT NULL`;
- `max_spins_per_phone integer NOT NULL`;
- `window_type varchar NOT NULL`;
- `window_n_days integer nullable`;
- `attempt_policy_id uuid NOT NULL` com FK para `dialing_attempt_policies`;
- checks para limites positivos, pessoa maior ou igual a telefone e coerência de `window_n_days`.

O sufixo `_v2` é deliberado para não colidir com a tabela histórica `dial_rules`.

#### `dialing_calendars`

- colunas comuns;
- `timezone varchar(64) NOT NULL`;
- `weekly_schedule jsonb NOT NULL`;
- `exceptions jsonb NOT NULL DEFAULT '[]'`;
- validação Pydantic de sete dias únicos, intervalos válidos e exceções sem datas duplicadas.

#### `dialing_spin_budgets`

- colunas comuns;
- `total_spins bigint NOT NULL`;
- `window_type varchar NOT NULL`;
- `window_n_days integer nullable`;
- checks equivalentes aos da janela da Regra.

#### `dialing_spin_budget_allocations`

- `id uuid PK`;
- `spin_budget_id uuid NOT NULL` com FK e `ON DELETE RESTRICT`;
- `flow_uuid uuid NOT NULL`;
- `label varchar(120) NOT NULL`;
- `weight numeric(5,2) NOT NULL`;
- `hard_cap_percent numeric(5,2) nullable`;
- unique `(spin_budget_id, flow_uuid)`;
- checks `0 < weight <= 100` e `weight <= hard_cap_percent <= 100` quando houver teto;
- soma igual a 100 validada transacionalmente pela aplicação.

#### `dialing_profiles`

- colunas comuns;
- `geography_source varchar NOT NULL`;
- `mailing_extra_field varchar nullable`;
- check que exige `mailing_extra_field` apenas quando a origem correspondente for usada.

#### `dialing_profile_mappings`

- `id uuid PK`;
- `profile_id uuid NOT NULL` com FK e `ON DELETE CASCADE`;
- `selector varchar(32) NOT NULL`;
- `is_default boolean NOT NULL DEFAULT false`;
- `dial_rule_id uuid NOT NULL` com FK para `dialing_rules_v2`;
- `calendar_id uuid NOT NULL` com FK para `dialing_calendars`;
- `spin_budget_id uuid NOT NULL` com FK para `dialing_spin_budgets`;
- unique `(profile_id, selector)`;
- índice único parcial garantindo no máximo um fallback por Perfil;
- a aplicação exige exatamente um fallback antes de publicar.

#### `dialing_resource_revisions`

- `id uuid PK`;
- `resource_type varchar NOT NULL` com check para os cinco tipos;
- `resource_id uuid NOT NULL`;
- `revision integer NOT NULL`;
- `snapshot jsonb NOT NULL`;
- `checksum varchar(128) NOT NULL`;
- `authored_by`, `published_by`, `created_at`, `published_at`;
- unique `(resource_type, resource_id, revision)`;
- snapshot de Perfil contém o agregado completamente resolvido.

#### `dialing_profile_effective_flows`

- `flow_uuid uuid PRIMARY KEY`;
- `profile_id uuid NOT NULL` com FK para `dialing_profiles`;
- `profile_revision_id uuid NOT NULL` com FK para `dialing_resource_revisions`;
- `bound_at timestamptz NOT NULL`;
- `bound_by varchar nullable`;
- materializa e garante uma única associação efetiva por campanha;
- sincronizada somente durante publish/archive do Perfil, sob lock transacional.

## Invariantes do domínio

- todos os identificadores referenciados pertencem ao mesmo workspace;
- `max_spins_per_person >= max_spins_per_phone >= 1`;
- janela `n_days` exige `1..365`; outras janelas exigem `NULL`;
- cada calendário possui sete dias únicos;
- intervalos não se sobrepõem e `start < end`;
- políticas não repetem evento;
- alocações somam exatamente 100%; tetos nunca são menores que pesos;
- Perfil possui exatamente um fallback;
- seletores de região não se repetem;
- Perfil publicado referencia somente recursos ativos e publicados;
- uma campanha possui somente um Perfil efetivo;
- simulação é read-only: deltas permanecem zero e nenhum contador é persistido.

## Solicitação executada para a migration

O pedido foi executado de forma aditiva, sem routers, modelos, CRUD, seeds,
integração com ORCH, backfill ou alteração da tabela `dial_rules`. O downgrade
destrutivo foi substituído pela política canônica fail-closed; rollback significa
não ativar consumidores e reverter o rollout, preservando as tabelas vazias para
auditoria.

## Implementação dos controladores após a migration

### Organização mínima

- `app/api/routers/dialing_management_v2.py` — HTTP e envelopes;
- `app/models/dialing_management_v2.py` — modelos Pydantic e invariantes;
- `app/services/dialing_management_v2.py` — transações, publicação, wizard e simulação;
- `app/crud/dialing_management_v2.py` — SQL parametrizado e isolado por schema;
- testes separados por modelo, API, serviço, persistência e router profiles.

O router de gestão pertence ao perfil `CRUD`. As rotas runtime de seleção/ciclos que serão criadas ao retomar o plano postergado pertencerão ao perfil `SUPPLIER`. Compartilhar namespace HTTP não significa misturar processos ou responsabilidades.

### Ordem de implementação

1. leitura/listagem dos cinco recursos;
2. create/update com `422` e concorrência otimista;
3. usage, archive, restore e clone;
4. publish e snapshots imutáveis;
5. criação atômica do wizard;
6. simulador read-only e explicável;
7. regressão completa de `/v1/dial-rules` e `/v1/contact-supplier`.

## UI operacional de Gestão de Extensões no `10.1.20.239`

### Regra arquitetural

A UI não recebe `DATABASE_URL` e não acessa PostgreSQL. Ela fala exclusivamente com um BFF/proxy restrito, que chama as APIs reais do Target Core. Somente o Target Core acessa a DB.

```text
Navegador via VPN + Basic Auth
        |
        v
UI operacional + BFF no 10.1.20.239
        |
        | Authorization interno + X-WORKSPACE-UUID permitido
        v
Target Core CRUD /v2/contact-supplier
        |
        v
schema real ws_<workspace_uuid>
```

### Controles obrigatórios

- stack própria em `/etc/gohp/dialing-management-demo`;
- units, runtime, logs e portas exclusivos; não instalar Docker apenas para
  hospedar esta demo no servidor Alpha;
- não reutilizar nem editar `.env` do Target Core;
- acesso somente pela rede interna/VPN, protegido por Basic Auth e pelo firewall
  existente; o BFF é o único listener da demo exposto na rede interna;
- token interno somente no servidor, fora da imagem e sem exposição no JavaScript;
- allowlist inicial de workspace, começando pelo workspace de teste aprovado;
- BFF com allowlist explícita de rotas e métodos; nada de proxy genérico;
- proteção CSRF nas mutações e headers de segurança;
- log de ator, workspace, recurso, revisão e ação para toda mutação;
- banner permanente **AMBIENTE REAL DE TESTES**;
- botão/ação destrutiva inexistente; apenas archive/restore;
- health checks separados;
- limites de CPU/memória para não disputar recursos com a Supplier;
- release versionada em diretório próprio e symlink `current` para rollback;
- nenhuma publicação antes de CRUD, migration e testes locais estarem verdes.

### Adaptações da UI

- substituir `NEXT_PUBLIC_MOCK_API_URL` por endpoint same-origin do BFF;
- interpretar o envelope `data/extra/meta` real;
- enviar revisão esperada nas mutações;
- exibir conflitos `409` e os `422` por campo;
- retirar textos “Mock API” e “Protótipo não produtivo” no modo real;
- manter banner e identificação inequívoca de demo/testes;
- impedir seleção de workspace fora da allowlist;
- preservar um modo `mock` local para desenvolvimento e um modo `real-demo` para o `.239`.

### Implantação segura

1. inventariar portas, Docker, proxy, disco e carga do `.239` em modo read-only;
2. homologar a UI contra API real em stack local e workspace descartável;
3. confirmar backup/rollback das novas tabelas de configuração;
4. instalar runtime e units isolados no `.239` sem reiniciar serviços existentes;
5. expor inicialmente apenas por túnel/rota interna controlada;
6. validar health, autenticação, workspace, leitura e uma criação em rascunho;
7. publicar um Perfil de canário e confirmar DB + API + visão 360º;
8. monitorar recursos e logs antes de abrir para a equipe;
9. rollback: parar somente as duas units da demo e preservar os dados para auditoria.

### Implantação confirmada em 2026-09-12

- release atual:
  `/etc/gohp/dialing-management-demo/releases/20260912T212950-randomuuid-fix`;
- entrada BFF/UI: `0.0.0.0:8300`, acessível somente pela rede interna/VPN e
  protegida por Basic Auth;
- UI interna: `127.0.0.1:8301`;
- units: `dialing-management-demo-ui.service` e
  `dialing-management-demo-bff.service`;
- runtime isolado: Node `22.17.0`, baixado do artefato oficial e validado por
  SHA-256;
- Target Core: `http://127.0.0.1:7501/v2/contact-supplier`;
- workspace inicial: `ba7eb0ec-e565-447c-8c11-8f870cf72a60`, com seletor por
  nome para os demais workspaces permitidos;
- BFF: token somente em arquivo server-side `0640`, allowlist de rotas/métodos,
  proteção de origem/CSRF, payload máximo de 1 MiB e headers de segurança;
- acesso humano pela VPN:

  ```bash
  playwright-cli open http://10.1.20.239:8300/
  ```

  A credencial deve ser informada no desafio Basic Auth do navegador e nunca na
  URL ou no repositório.

A página, a API real e o acesso autenticado responderam `200`; acesso sem
credencial respondeu `401`. As duas units permaneceram `active/running`, com
zero restart e sem erros no journal da janela final. A inspeção visual com
Playwright CLI cobriu desktop e viewport móvel.

### Extensão implantada — Listas de Restrição

A release `20260912T211500-restrictions` adiciona, sem acesso direto ao banco:

- dashboard de listas com estados `draft`, `active` e `archived`;
- criação, edição, ativação, arquivamento e restauração sem delete físico;
- inclusão manual de identificador, motivo, referência e validade;
- exibição exclusiva de valores mascarados;
- upload CSV de até 20 MiB com `Idempotency-Key`;
- escolha entre `append_upsert` e `replace_active`;
- mapeamento de colunas para identificadores e metadados, com sugestão inicial
  e prévia mascarada;
- acompanhamento dos estados do job, percentual e contadores de lidas, válidas,
  rejeitadas, ignoradas, criadas, atualizadas e inativadas;
- cancelamento antes do processamento, reprocessamento de falha e relatório
  seguro quando disponibilizado pela API;
- atualização automática enquanto houver job em processamento.

O BFF permanece fail-closed: somente as rotas explícitas de Listas de Restrição
foram adicionadas; multipart e `Idempotency-Key` são preservados, mutações
continuam exigindo origem/CSRF e o workspace é validado antes do encaminhamento.
No runtime real, o endpoint de listas retornou `data[]` e `field-options`
retornou 22 campos. A UI/BFF passou por lint, build, verificação sintática e
smoke de autenticação, workspace, rotas, CSRF e multipart antes da troca
atômica do symlink. A release anterior permanece disponível para rollback.

Em 2026-09-12, o upload CSV foi corrigido para funcionar também no acesso HTTP
interno da demo. `Crypto.randomUUID()` depende de contexto seguro e não existe
na origem `http://10.1.20.239:8300`; quando indisponível, a UI agora monta um
UUID v4 com `Crypto.getRandomValues()`, sem recorrer a aleatoriedade fraca. O
Playwright reproduziu o erro original e, após a release
`20260912T212950-randomuuid-fix`, observou o POST multipart com
`Idempotency-Key` UUID v4 válido. O POST foi interceptado no navegador, de modo
que a validação não criou job nem alterou a lista real. A release
`20260912T211500-restrictions` permanece como rollback imediato.

Em 2026-09-13, a homologação real da importação foi concluída na lista
`1baf8814-72c5-4b27-b307-b9bffce70e91`. O job
`0f42c07e-3826-4b2e-b678-0e7a95b96b17` terminou com três linhas válidas, zero
rejeitadas, zero ignoradas e dez entradas materializadas; o job
`2ee09416-b88f-4bcc-abf5-313c5c577db5` terminou com uma linha válida, zero
rejeitadas e quatro entradas criadas. A rota exclusiva do perfil Supplier
devolveu `restricted` para um canal importado e `allowed` para o controle.

O consumidor ORCH usa o componente `check_restriction_lists`, com escopo
`person|current_channel`, multi-select de listas ativas e branches exatas
`restricted|allowed`. O resultado salvo no runtime contém somente decisão,
contagem, identificadores das listas, campos avaliados e ocorrências
mascaradas. Falha HTTP, configuração incompleta ou resposta inconsistente
terminaliza sem percorrer `allowed`.

A URL runtime precisa apontar explicitamente para o perfil Supplier por
`TARGET_CORE_SUPPLIER_API_BASE_URL`. Não reutilizar `TARGET_CORE_API_BASE_URL`:
no ambiente atual ela aponta para o perfil CRUD e a tentativa de chamar o
evaluator por essa origem devolveu corretamente `405 Method Not Allowed`.

A regressão local posterior subiu API, workers e beats de todas as fases com as
filas isoladas `*_f5_local`; o smoke canônico aceitou cinco sessões em cada um
dos dois flows de referência. A fila FileApp local já continha backlog de outro
workspace (`f0d1…`) e produziu timeouts na integração antiga enquanto a stack
estava ativa. Esse ruído não pertenceu aos flows do smoke nem ao novo card; a
stack criada para o teste foi encerrada para não continuar consumindo o backlog.

### Extensão implantada — Telecom / Troncos

Em 2026-09-13, a release
`/etc/gohp/dialing-management-demo/releases/20260913T105323-telecom-crud`
substituiu a reserva visual de **Telecom → Troncos** por uma tela real ligada à
fachada Target Core `/v2/telecom/trunks`.

A entrega inclui:

- inventário pesquisável por nome, UUID, DID ou campanha;
- resumo de troncos padrão, MetaSip, DIDs e campanhas;
- detalhe normalizado de autenticação por IP ou usuário, sem senha;
- consulta de disponibilidade sob demanda, tratando ausência no monitor como
  `Sem telemetria`;
- formulário guiado para criar tronco por IP ou usuário/senha e vincular DIDs;
- edição parcial por `PATCH`, sem exigir ou revelar a senha SIP atual;
- contratos BFF para substituição integral por `PUT` e remoção por `DELETE`;
- confirmação forte de exclusão pelo nome do tronco, com contagem de DIDs e
  campanhas exibida antes da ação;
- aviso explícito de que a criação materializa infraestrutura real.

O CRUD é funcional também para os troncos preexistentes; a restrição de não os
alterar ou remover pertence somente ao procedimento de desenvolvimento. O BFF
permite `GET`, `POST`, `PUT`, `PATCH` e `DELETE` nas rotas exatas de troncos,
exigindo mesma origem e o cabeçalho CSRF para qualquer mutação. A interface
respeita `capabilities.can_edit` e `capabilities.can_delete` devolvidos pela
fachada: na fotografia real, os dois troncos `standard` permitem edição e
exclusão, enquanto o `MetaSip` permite edição mas não exclusão.

O smoke real pelo BFF retornou os três troncos do workspace aprovado, não expôs
senha e recusou com `403` um `PATCH` fictício sem CSRF, antes de alcançar o
Target Core. Os caminhos mutáveis completos foram exercitados somente contra o
mock local/isolado. Nenhum `POST`, `PUT`, `PATCH` ou `DELETE` foi executado no
Pool durante o deploy.

Lint, TypeScript, build Vinext e smoke isolado do BFF passaram. As units
`dialing-management-demo-ui.service` e
`dialing-management-demo-bff.service` ficaram `active`, com zero restart e sem
warnings no journal após a troca atômica. A release anterior
`20260913T080118-extensions-menu` permanece disponível para rollback seguro.
Durante o primeiro staging do CRUD, `cp -a` preservou o symlink da release
`20260913T102705-telecom-trunks` e ela recebeu o mesmo build antes da correção;
portanto, ela não deve ser tratada como rollback distinto. A release ativa
`20260913T105323-telecom-crud` foi depois materializada como diretório físico e
confirmada por `readlink -f`.

O primeiro `POST` real revelou um contrato parcial da API Pool: a escrita foi
concluída com `201`, mas o serializer de escrita não devolveu o recurso completo
e a fachada Target respondeu `502`. Dois troncos de teste ficaram materializados
durante as tentativas e não foram alterados nem removidos no diagnóstico. A PR
Target Core `#492` introduziu recuperação somente na fachada
`/v2/telecom/trunks`: criação relê por nome exato; `PUT`/`PATCH` releem pelo UUID
conhecido. A v1 legada não foi modificada.

Depois do merge `0f39dcb`, a release da UI
`20260913T125615-telecom-response-ui` passou a limitar a edição de MetaSip aos
DIDs, refletindo o contrato real da Pool. UI/BFF ficaram `active/running`, sem
restarts ou warnings; health e `GET /api/telecom/trunks` responderam `200`.
Nenhuma mutação foi executada nesse deploy.

O build dessa release confirmou uma armadilha operacional recorrente: mesmo
chamando o `npm-cli.js` com Node 22, o script `vinext` resolveu o Node global
`18.19.1` e falhou em `node:util.styleText`. O build correto chama diretamente
o CLI Vinext com o Node dedicado `22.17.0`. O procedimento canônico e a
pendência de criação do repositório Git da UI estão em
`DIALING_MANAGEMENT_UI_RUNBOOK.md`.

### Procedimento canônico de inspeção visual com Playwright CLI

O caminho operacional aprovado para esta UI é o CLI oficial do Playwright. A
instalação global é feita apenas quando `playwright-cli --version` não estiver
disponível:

```bash
npm install -g @playwright/cli@latest
```

Para abrir a UI atual pela VPN:

```bash
playwright-cli open http://10.1.20.239:8300/
```

Quando o agente precisar manter comandos subsequentes (`snapshot`, `click`,
`fill`), usar uma sessão nomeada com perfil persistente:

```bash
playwright-cli -s=dialing open http://10.1.20.239:8300/ --persistent
playwright-cli -s=dialing snapshot
```

Manter o terminal que abriu a sessão vivo durante toda a inspeção. Informar a
credencial no desafio Basic Auth do navegador; não embuti-la na URL.

### Ajustes operacionais aprovados para a UI

1. Campanhas são opcionais no wizard e no Orçamento, inclusive quando
   `share_budget=true`. A soma de pesos igual a 100% só é exigida quando houver
   ao menos uma campanha selecionada.
2. O usuário escolhe flows por nome. O UUID permanece apenas como informação
   secundária e como valor persistido.
3. Um flow efetivamente vinculado a outro Perfil deve aparecer identificado e
   indisponível no wizard. Na edição normal, os vínculos do próprio Perfil
   continuam visíveis e removíveis.
4. O cabeçalho oferece troca de workspace por nome; o UUID continua sendo
   enviado ao BFF e nunca é aceito como nome de schema.
5. A escolha regional começa por País e carrega estados/províncias reais por
   código ISO. Nesta etapa, País é um auxílio de configuração da UI; o contrato
   persistido continua usando o seletor de subdivisão já suportado pelo Perfil.
6. O BFF deixa de escutar apenas em loopback, passa a ser acessível pela rede
   interna/VPN e exige uma credencial compartilhada. Token do Target Core e
   credenciais de banco permanecem exclusivamente server-side.
7. Salvar um draft apenas valida e persiste `dial_profile_id`. O vínculo na
   tabela `dialing_profile_effective_flows` deve acompanhar a revisão
   executável no publish/rollback, nunca uma alteração de draft ainda não
   publicada.

### Inspeção visual canônica com Playwright CLI

Para esta UI, usar o `playwright-cli` quando for necessário manter
uma janela visível que o usuário possa posicionar e o agente possa continuar
inspecionando. A instalação global é necessária somente quando o binário ainda
não existir:

```bash
npm install -g @playwright/cli@latest
```

Antes de reinstalar, verificar `playwright-cli --version`. Com a VPN ativa,
abrir a UI com:

```bash
playwright-cli open http://10.1.20.239:8300/
```

Reutilizar a janela/sessão aberta durante a inspeção; não reiniciar o navegador
entre cada etapa do wizard. Credenciais de acesso não devem ser incluídas na
URL nem registradas neste documento.

## Testes obrigatórios

### Migration

- criação em schema novo;
- upgrade de workspace existente sem alteração em `dial_rules`;
- checks, índices, FKs e tablespace;
- downgrade recusado de forma fail-closed;
- cadeia completa e helper real de provisionamento de workspace novo em
  PostgreSQL descartável;
- emissão de ACK terminal coberta pelo teste isolado da task; um E2E real do
  pipeline completo permanece obrigatório antes do rollout geral.

### API e domínio

- isolamento entre workspaces;
- autenticação obrigatória;
- paginação e filtros;
- `422` por campo para cada invariante;
- conflito de revisão concorrente;
- publicação imutável;
- recurso compartilhado e consulta de uso;
- archive bloqueado quando em uso;
- wizard sem estado parcial em qualquer falha;
- unicidade de Perfil efetivo por `flow_uuid`;
- simulação sem escrita;
- regressão das rotas/tabela legadas.

### E2E da UI real

- criar configuração pelo wizard;
- abrir visão 360º com todas as referências;
- editar e publicar conscientemente um recurso compartilhado;
- simular `allow`, `defer` e `block`;
- confirmar que o seletor do card retorna apenas Perfil ativo/publicado;
- verificar registros reais no workspace de teste;
- provar que nenhuma tabela legada foi modificada.
- para Listas de Restrição: criar rascunho, incluir entrada manual, ativar,
  importar CSV pequeno nos dois modos, acompanhar o job até terminal e conferir
  o relatório de rejeições sem expor valores brutos.

## Sequência final do projeto

1. **Concluído:** protótipo visual, wizard e visão 360º.
2. **Concluído:** contrato aprovado e migration criada/testada em branch
   isolado, inclusive em workspace existente e em workspace descartável novo.
3. **Concluído:** migration mergeada pela PR Target Core `#483` e distribuída
   nos 59 workspaces ativos.
4. **Concluído:** CRUD real mergeado pela PR Target Core `#484` e implantado no
   processo CRUD do `.239`.
5. **Concluído:** UI adaptada ao envelope real e BFF fail-closed homologado.
6. **Concluído:** UI operacional implantada no `.239`, por entrada autenticada
   na rede interna/VPN,
   com canário real criado e arquivado.
7. **Concluído:** catálogo do novo card alterado para selecionar
   `dial_profile_id`, com flow de homologação salvo e publicado.
8. **Concluído:** Lista de Restrição e importações reais homologadas pela UI;
   evaluator confirmou os casos `restricted` e `allowed` no perfil Supplier.
9. **Atual:** publicar catálogo/422 e engine ORCH do card
   `check_restriction_lists`, configurar a URL exclusiva do Supplier e
   homologar os dois branches em um flow pequeno.
10. Introduzir o card no fluxo geral
    `c1dfbaa3-41c6-41b5-bf50-b7f6ba5c5152` após o canário.
11. Adaptar o plano `POSTERGADO_SUPPLIER_V2_DIAL_RULE.md` ao snapshot do Perfil.
12. Implementar ciclos, feedback, retentativas e decisão terminal da Supplier V2.
13. Exercitar o fluxo completo real sem tocar no discador legado.

## Rollback

- CRUD: desabilitar/inibir o router V2; V1 permanece intacto;
- UI: parar somente `dialing-management-demo-bff.service` e
  `dialing-management-demo-ui.service`; para rollback de release, reposicionar
  o symlink `/etc/gohp/dialing-management-demo/current` e reiniciar somente
  essas units;
- dados: preservar tabelas e revisões para auditoria; não apagar automaticamente;
- card: não publicar catálogo consumidor até o CRUD estar homologado;
- runtime: sem consumidor do Perfil, nenhuma regra V2 afeta a Supplier atual.
