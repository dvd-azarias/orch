# Handoff técnico-funcional — UI de Extensões do Target

Status: referência para implementação pela equipe oficial de UI

Data de consolidação: 2026-09-14

Ambiente de referência: interno/homologação

Classificação no Alpha: `ALPHA_FIX_OPTIONAL` — transferência de conhecimento e redução de risco operacional

## 1. Objetivo

Este documento descreve, de forma autocontida, o produto, as jornadas, os contratos HTTP e os critérios de qualidade da UI de Extensões construída para exercitar funcionalidades reais do Target Core.

A equipe oficial de UI deve conseguir reproduzir o conceito integralmente, em especial:

- a navegação hierárquica de Extensões;
- a visão 360 dos Perfis de Discagem;
- a criação assistida por wizard;
- a gestão de recursos associados ao perfil;
- a gestão e importação de Listas de Restrição;
- a gestão de Troncos e Rotas de Telecom;
- as proteções de segurança, privacidade, concorrência e consistência já praticadas.

O objetivo não é copiar literalmente uma aplicação temporária. É transportar para a UI oficial as decisões de produto e os comportamentos já validados, preservando a identidade visual e a arquitetura de autenticação da aplicação oficial.

## 2. Fontes da verdade e baseline

### 2.1 Código da UI de referência

- Repositório: `https://github.com/GOHP-LAB/target-extensions-ui`
- Branch de referência: `main`
- Baseline conferido para este documento: commit `f9d2a3d`
- Node.js usado no ambiente de referência: `22.17.0`

Arquivos especialmente relevantes:

- `components/dialing-studio.tsx`: navegação e composição geral;
- `components/guided-wizard.tsx`: wizard de Perfil de Discagem;
- `components/profile-360.tsx`: visualização 360;
- `components/calendar-exceptions.tsx`: datas especiais reutilizáveis;
- `components/restriction-lists.tsx`: listas, busca e importações;
- `components/telecom-trunks.tsx`: inventário e edição de troncos;
- `components/telecom-routes.tsx`: inventário e wizard de rotas;
- `components/timezone-field.tsx`: seleção de timezone IANA;
- `lib/api.ts`: cliente HTTP usado pela UI;
- `lib/types.ts`: contratos utilizados no frontend;
- `real-demo/bff.mjs`: BFF, autenticação e proxy seguro;
- `openapi/dialing-management.yaml`: referência OpenAPI complementar.

### 2.2 APIs e comportamento de domínio

O comportamento real do Target Core, seus modelos e testes prevalece sobre exemplos antigos. As famílias atuais são:

- Gestão de Discagem: `/v2/contact-supplier/*`;
- Listas de Restrição: `/v2/contact-supplier/restriction-lists/*`;
- Telecom: `/v2/telecom/*`;
- Fluxos para seleção amigável: `/v2/flow?for_list=true...`;
- Workspaces permitidos: `/v1/workspaces`.

Não reintroduzir os conceitos antigos de “Dial Rule” e “Orçamento de Spins” como entidades separadas. O modelo vigente usa **Limites Compartilhados de Tentativas**, contendo limites por pessoa, limites por telefone, período de reset e, opcionalmente, distribuição entre campanhas.

## 3. Ambiente de referência e acesso

### 3.1 Aplicação disponível para consulta visual

- URL interna: `http://10.1.20.239:8300/`
- Requisito: rede interna ou VPN corporativa;
- Proteção atual: HTTP Basic;
- Workspace usado como canário: `ba7eb0ec-e565-447c-8c11-8f870cf72a60`;
- Flow usado na homologação do novo discador: `4e163399-e9a0-4335-895f-316c6a161299`;
- Perfil Bradesco válido usado na homologação: `2c2999b3-46f6-4d94-a60a-3647436d7220`.

O nome histórico do serviço contém `dialing-management-demo`, mas a aplicação passou a ser uma ferramenta operacional real. Ela não deve ser tratada como descartável.

### 3.2 Credenciais

Nenhuma senha, token ou segredo deve ser gravado neste documento, no repositório, em código frontend, em URL ou em exemplos de `curl`.

Credenciais necessárias no ambiente de referência:

| Uso | Identificador/configuração | Forma correta de entrega |
| --- | --- | --- |
| Abrir o site | `DEMO_BASIC_AUTH_USER` e `DEMO_BASIC_AUTH_PASSWORD` | cofre corporativo ou canal seguro |
| BFF chamar Target Core | `TARGET_BEARER_TOKEN_FILE` | arquivo de secret somente no servidor |
| Ator técnico | `TARGET_USER_UUID` | variável de ambiente do BFF |
| Workspace inicial | `TARGET_WORKSPACE_UUID` | variável de ambiente, sem ser segredo |

No servidor de referência, a configuração operacional fica em:

- `/etc/gohp/dialing-management-demo/dialing-management-demo.env`;
- `/etc/gohp/dialing-management-demo/secrets/target.token`.

Esses caminhos são informação operacional, não autorização para copiar ou exibir seus conteúdos. A equipe deve solicitar a credencial ao responsável pelo ambiente. A UI oficial deve usar a sessão/JWT já estabelecida pelo produto e nunca receber um token técnico do Target Core no browser.

### 3.3 Inspeção com Playwright CLI

Procedimento canônico já validado:

```bash
npm install -g @playwright/cli@latest
playwright-cli open http://10.1.20.239:8300/
```

O prompt de HTTP Basic será apresentado pelo browser. Não colocar usuário e senha na URL nem registrar a credencial em capturas, scripts ou documentos.

### 3.4 Execução local do projeto de referência

No repositório `target-extensions-ui`:

```bash
docker compose up --build -d
```

O ambiente local documentado pelo projeto expõe a UI em `http://localhost:3000` e a API mock em `http://localhost:8787`. Conferir o `README.md` do repositório antes de iniciar, pois o ambiente real usa o BFF descrito a seguir.

### 3.5 Documentação interativa das APIs

O Target Core usa FastAPI e mantém o Swagger padrão em `/docs` no serviço correspondente. Referências usuais:

- no próprio host do CRUD: `http://127.0.0.1:7501/docs`;
- pelo ingresso corporativo, quando a política do gateway permitir: `https://sync-core-api.otima.io/target/docs`.

O Swagger pode estar indisponível externamente por regra de ingresso. Nesse caso, os routers, schemas e testes do `main` do Target Core são a fonte autoritativa; não recorrer à API V1 do Pool como substituta dos contratos V2 descritos aqui.

## 4. Arquitetura de integração

```text
Browser
  |
  | HTTP Basic no ambiente temporário
  | sessão/JWT na UI oficial
  v
BFF de Extensões :8300
  |-- UI upstream :8301
  |-- /api/dialing  -> Target Core /v2/contact-supplier
  |-- /api/telecom  -> Target Core /v2/telecom
  `-- /api/context  -> workspaces e flows permitidos
```

Princípios obrigatórios:

1. O browser não acessa banco de dados.
2. O browser não recebe token técnico do Target Core.
3. O BFF injeta autenticação técnica e contexto do ator.
4. O workspace de cada chamada é explícito e validado contra a lista de workspaces permitidos ao usuário.
5. Mutations são protegidas por sessão, origem e CSRF.
6. O BFF é `fail closed`: se não puder validar autorização/contexto, a operação não prossegue.
7. A UI nunca chama diretamente a aplicação interna Pool/Telecom. O Target Core V2 é a fronteira pública desse domínio.

Variáveis principais do BFF de referência:

| Variável | Finalidade |
| --- | --- |
| `LISTEN_HOST` | interface de escuta, atualmente `0.0.0.0` |
| `LISTEN_PORT` | porta pública, atualmente `8300` |
| `UI_UPSTREAM` | UI local, normalmente `http://127.0.0.1:8301` |
| `TARGET_BASE_URL` | base server-side de Gestão de Discagem |
| `TARGET_TELECOM_BASE_URL` | base server-side de Telecom V2 |
| `TARGET_WORKSPACE_UUID` | workspace inicial |
| `TARGET_USER_UUID` | ator técnico/auditável |
| `TARGET_BEARER_TOKEN_FILE` | secret do Target Core |
| `DEMO_BASIC_AUTH_USER` | usuário do site temporário |
| `DEMO_BASIC_AUTH_PASSWORD` | senha do site temporário |

No host do Target Core, a base server-side usual é `http://127.0.0.1:7501`. Para integrações externas autorizadas, a base pública observada é `https://sync-core-api.otima.io/target`. A UI oficial deve usar o gateway/BFF definido pela arquitetura do produto, não fixar nenhum desses hosts no bundle.

## 5. Cabeçalhos e envelope HTTP

### 5.1 Browser para o BFF de referência

Todas as chamadas:

```http
X-Dialing-Demo-Workspace: <workspace_uuid>
```

Chamadas que alteram estado também usam:

```http
X-Dialing-Demo-CSRF: 1
```

O cliente de referência adiciona esse cabeçalho automaticamente. Na UI oficial, usar o mecanismo de CSRF e sessão já homologado pela plataforma.

### 5.2 BFF/serviço autorizado para o Target Core

```http
Authorization: Bearer <token obtido por canal seguro>
X-WORKSPACE-UUID: <workspace_uuid>
X-User-UUID: <actor_uuid>
Content-Type: application/json
Accept: application/json
```

Exemplo sanitizado:

```bash
curl --fail-with-body \
  "$TARGET_API_BASE/v2/contact-supplier/dial-profiles?per_page=100" \
  -H "Authorization: Bearer $TARGET_TOKEN" \
  -H "X-WORKSPACE-UUID: $WORKSPACE_UUID" \
  -H "X-User-UUID: $ACTOR_UUID" \
  -H 'Accept: application/json'
```

Nunca salvar o token real no shell history, Postman exportado, repositório ou ticket. Preferir secret local não versionado ou cofre da ferramenta de API.

### 5.3 Envelopes de resposta

Recurso individual:

```json
{
  "data": {
    "id": "uuid",
    "name": "Nome do recurso"
  }
}
```

Coleção paginada:

```json
{
  "data": [],
  "extra": {},
  "meta": {
    "current_page": 1,
    "per_page": 20,
    "total": 0,
    "total_pages": 0
  }
}
```

Erro de validação `422`:

```json
{
  "message": "Erro de validação nos dados enviados",
  "errors": {
    "campo": ["Mensagem específica e acionável."]
  },
  "ui_message": "Erro de validação",
  "error_code": "codigo_estavel_do_erro",
  "meta": {}
}
```

A UI deve associar `errors.<campo>` ao controle correspondente e também mostrar um resumo no topo. Não reduzir um 422 detalhado a um toast genérico.

## 6. Arquitetura de informação e menu

Estrutura validada:

```text
Extensões
  Discador
    Perfis de Discagem
      Calendários
      Datas de exceção
      Políticas de Tentativas
      Limites Compartilhados de Tentativas
    Criação assistida
    Simulador
  Listas de Restrição
    Listas e importações
  Telecom
    Troncos
    Rotas
```

Regras de navegação:

- somente `Extensões` aparece no primeiro nível;
- apenas um domínio principal fica expandido por vez;
- a árvore de Perfis de Discagem pode ser recolhida;
- o botão de expansão/recolhimento precisa ser óbvio, consistente e acessível;
- em viewport largo, usar sidebar;
- abaixo de aproximadamente `1280 px`, substituir a árvore por seletor compacto ou drawer funcional;
- filtros pertencem à tela atual e são descartados na troca de módulo;
- o workspace atual aparece no topo com nome legível; o UUID é informação secundária;
- a seleção de workspace persiste localmente para conveniência, mas cada request continua enviando o UUID explicitamente.

### 6.1 URLs

A UI de referência atual é uma SPA e usa apenas `/`. Portanto, URLs como `/extensions/dialer/profiles` ainda não existem naquele site.

Para a UI oficial, recomenda-se deep linking:

| Tela | Rota sugerida para a UI oficial |
| --- | --- |
| Perfis | `/extensions/dialer/profiles` |
| Visão 360 | `/extensions/dialer/profiles/:profileId` |
| Criação assistida | `/extensions/dialer/wizard` |
| Simulador | `/extensions/dialer/simulator` |
| Calendários | `/extensions/dialer/calendars` |
| Datas de exceção | `/extensions/dialer/calendar-exceptions` |
| Políticas | `/extensions/dialer/attempt-policies` |
| Limites compartilhados | `/extensions/dialer/attempt-limits` |
| Listas de Restrição | `/extensions/restrictions/lists` |
| Detalhe da lista | `/extensions/restrictions/lists/:listId` |
| Troncos | `/extensions/telecom/trunks` |
| Detalhe do tronco | `/extensions/telecom/trunks/:trunkId` |
| Rotas | `/extensions/telecom/routes` |
| Detalhe da rota | `/extensions/telecom/routes/:routeId` |

Essas rotas são recomendação de produto para a implementação oficial, não contrato do site de referência.

## 7. Linguagem visual e experiência compartilhada

A identidade visual definitiva pertence à UI oficial. O conceito validado, contudo, deve ser preservado:

- fundo claro e pouco ruidoso;
- navegação em azul-marinho escuro;
- índigo para ações e estados primários;
- coral/laranja para destaques e chamadas de ação;
- cartões com hierarquia clara, bordas suaves e espaços generosos;
- textos em português direto, explicando o efeito operacional antes do termo técnico;
- UUIDs sempre secundários e copiáveis, nunca usados como rótulo principal;
- confirmação forte para ações destrutivas ou de grande impacto;
- skeleton ou indicador de carregamento no lugar de telas congeladas;
- empty state explicando o próximo passo;
- feedback de sucesso com próximo caminho natural;
- erro preservando o que o usuário já digitou.

Requisitos de acessibilidade:

- navegação integral por teclado;
- foco visível;
- rótulo programático para controles e ícones;
- `aria-expanded` nos grupos recolhíveis;
- contraste compatível com WCAG AA;
- mensagens de erro associadas a seus campos;
- stepper do wizard com etapa atual anunciável;
- não usar somente cor para status;
- modais com retenção de foco e retorno ao acionador.

## 8. Conceitos do domínio de Discagem

### 8.1 Perfil de Discagem

Agregado que reúne:

- uma Política de Tentativas;
- um conjunto de Limites Compartilhados de Tentativas;
- a fonte usada para resolver geografia;
- calendários associados a regiões;
- um calendário fallback obrigatório.

Um flow possui no máximo um perfil efetivo. Um perfil pode ser compartilhado por vários flows.

### 8.2 Política de Tentativas

Define, para cada resultado de telefonia:

- se o evento consome tentativa;
- máximo específico do resultado;
- intervalo antes de nova tentativa;
- ação ao atingir o limite.

Eventos suportados:

- `ringing`;
- `busy`;
- `machine`;
- `no_answer`;
- `technical_failure`;
- `invalid_number`.

Ações suportadas:

- `next_phone` — tentar outro telefone;
- `finish_person` — encerrar o tratamento da pessoa;
- `pause_person` — pausar a pessoa;
- `block_phone` — bloquear o telefone.

### 8.3 Limites Compartilhados de Tentativas

É a única entidade para o orçamento/contagem compartilhada. Define:

- máximo por pessoa;
- máximo por telefone;
- período de reset;
- timezone IANA do reset;
- campanhas participantes;
- peso elástico e teto rígido opcional de cada campanha.

Invariantes:

- `max_attempts_per_person >= max_attempts_per_phone >= 1`;
- campanhas são opcionais;
- quando houver campanhas, os pesos totalizam exatamente `100`;
- `hard_cap_percent`, quando informado, não pode ser menor que `weight`;
- uma campanha aparece uma única vez;
- uma campanha só pode ter um perfil efetivo.

Períodos de reset:

- `calendar_day` — dia-calendário no timezone configurado;
- `rolling_24h` — 24 horas corridas;
- `calendar_week` — semana de calendário;
- `n_days` — quantidade configurável, de 1 a 365;
- `never` — não reinicia.

### 8.4 Calendário de Discagem

Define timezone IANA, os sete dias da semana, faixas horárias não sobrepostas e exceções locais. Um dia habilitado precisa ter ao menos uma faixa; um dia desabilitado não pode ter faixas.

### 8.5 Data de exceção reutilizável

Feriado ou data especial criada uma vez e vinculada a zero ou mais Perfis de Discagem. Ao vinculá-la, o backend a propaga aos calendários que compõem os perfis.

Modos:

- `closed` — operação fechada, sem faixas;
- `custom_hours` — horário especial, com ao menos uma faixa.

Conflitos com exceção local existente na mesma data produzem 422 específico. A UI deve mostrar o aviso antes de tentar salvar quando já tiver informação suficiente e deve apresentar o detalhamento devolvido pelo backend.

### 8.6 Revisões e publicação

Os recursos possuem:

- `status`: `draft`, `published` ou `archived`;
- `revision`: revisão editável atual;
- `published_revision`: última revisão publicada.

A publicação cria/referencia uma revisão imutável do agregado. Uma edição posterior incrementa `revision` e pode deixar `published_revision < revision` até nova publicação.

Mutations concorrentes devem enviar revisão esperada por `If-Match` e/ou `expected_revision`. Em `409`, não sobrescrever silenciosamente: informar que o recurso mudou, oferecer recarregar e preservar uma cópia local do que foi digitado.

## 9. Tela de Perfis de Discagem

### 9.1 Inventário

Requisitos:

- alternância **Cards / Tabela**;
- preferência persistida no browser;
- busca por nome;
- filtros por `draft`, `published`, `archived` e estado ativo;
- ação primária “Novo Perfil” conduzindo à Criação Assistida;
- clique no card/linha abre a visão 360;
- nome em destaque, status e revisão visíveis;
- ações sensíveis respeitam `usage` e o status do recurso.

Não é necessária uma tela separada “Visão Geral”. O inventário e a visão 360 cumprem esse papel.

### 9.2 Visão 360

Cabeçalho:

- nome e descrição;
- status;
- revisão atual e publicada;
- `dial_profile_id` copiável;
- ações publicar, arquivar, restaurar e clonar quando permitidas;
- resumo de campanhas vinculadas.

Conteúdo:

1. Banner de integridade, alertando referências ausentes ou revisões não publicadas.
2. Grade “Comportamento geral e limites” mostrando a Política de Tentativas e os Limites Compartilhados selecionados.
3. Grade “Configuração por região” com seletor geográfico e o Calendário associado.
4. Fallback nacional/default sempre visível.

Regra de edição validada:

- a visão 360 edita **referências** do perfil;
- ela permite trocar qual política, limite ou calendário é associado;
- ela não edita inline o conteúdo desses recursos;
- para modificar horários de um calendário, abrir o módulo Calendários;
- para modificar resultados, abrir Políticas de Tentativas;
- para modificar contagem/reset/alocação, abrir Limites Compartilhados.

Esse desenho evita controles duplicados e reduz ambiguidade entre “trocar o recurso” e “editar o recurso”.

## 10. Submódulos de um Perfil

Calendários, Políticas de Tentativas e Limites Compartilhados não devem listar tudo ao entrar.

Fluxo:

1. Usuário escolhe um Perfil de Discagem em combo por nome.
2. A tela carrega somente os recursos usados por esse perfil.
3. Deve existir uma opção explícita para “Recursos sem vínculo”, quando a gestão desses itens for necessária.
4. UUID é secundário.

Isso mantém contexto e evita inventários globais difíceis de compreender.

## 11. Wizard — Criação Assistida de Perfil

Esta é a principal referência de experiência para novos wizards do produto.

### 11.1 Entrada e comportamento geral

- Entrar em “Criação assistida” abre uma página de apresentação, não o formulário imediatamente.
- O botão **Criar novo Perfil de Discagem** inicia o wizard.
- Cancelar na primeira etapa retorna à apresentação.
- Nada é persistido antes da confirmação final.
- Stepper e progresso ficam visíveis.
- Cada etapa contém uma pergunta em linguagem simples, uma explicação curta e somente os campos necessários naquele momento.
- Decisões Sim/Não usam cards ou botões grandes.
- Campos condicionais aparecem por progressive disclosure.
- “Voltar” preserva dados já informados.
- “Continuar” fica bloqueado enquanto a etapa não for válida.
- A etapa final resume efeitos e nomes dos recursos que serão criados.
- O submit é atômico por um único endpoint.
- O sucesso oferece: abrir visão 360, abrir simulador e visualizar como o perfil aparecerá no card do flow.

### 11.2 Etapas

#### Etapa 1 — Objetivo

Coletar:

- nome;
- descrição opcional;
- intenção de publicar imediatamente ou salvar rascunho.

#### Etapa 2 — Campanhas

Pergunta: o perfil será compartilhado agora por campanhas?

- Campanha não é obrigatória, inclusive quando o usuário escolhe “Sim, compartilhar”.
- Usuário pode criar o perfil para uso posterior no card.
- Listar flows por nome; UUID aparece apenas como apoio/cópia.
- Flows que já possuem perfil efetivo aparecem desabilitados e explicam o motivo.
- No cadastro normal, um vínculo existente pode ser removido/trocado conforme permissão.
- Para cada campanha, permitir peso elástico e teto rígido opcional.
- Se houver campanhas, soma dos pesos deve ser exatamente 100%.

Quando o perfil for selecionado posteriormente no card do canvas, o `dial_profile_id` integra o definition salvo do flow; o backend deve garantir que o vínculo efetivo permaneça único.

#### Etapa 3 — Geografia

Perguntas:

1. O perfil precisa de tratamento regional?
2. Qual dado determina a região?

Fontes:

- UF declarada no contato: `contact_state`;
- UF do endereço: `address_state`;
- UF inferida pelo DDD: `inferred_by_area_code`;
- campo extra do mailing: `mailing_extra_field`.

Quando usar campo extra, seu nome é obrigatório.

Na seleção regional:

- selecionar primeiro o país;
- listar estados/províncias reais do país escolhido;
- disponibilizar **Selecionar todos** e **Limpar seleção**;
- mostrar quantidade selecionada;
- preservar fallback obrigatório.

A referência usa dados de país/estado compatíveis com `@countrystatecity/countries-browser`. A UI oficial pode usar sua biblioteca homologada, desde que trabalhe com códigos ISO reais e não mantenha uma lista manual incompleta.

#### Etapa 4 — Horários

Defaults atuais:

- segunda a sexta: `09:00`–`18:00`;
- sábado: fechado por padrão, sugestão `10:00`–`14:00` quando habilitado;
- domingo: fechado;
- timezone: `America/Sao_Paulo`.

No modo regional, a configuração gera um calendário por região selecionada e um fallback. Isso é intencional: horários e feriados podem variar por estado. Não gerar uma Política de Tentativas ou um Limite por estado.

#### Etapa 5 — Datas especiais

Permitir:

- não usar datas especiais;
- selecionar exceções globais existentes;
- preparar uma nova data especial sem sair do wizard;
- informar nome, descrição, data, fechado ou horário especial;
- vincular a nova exceção aos calendários do perfil criado.

Novas exceções preparadas nesta etapa só são persistidas no submit final atômico.

#### Etapa 6 — Tentativas

Coletar:

- máximo por pessoa;
- máximo por telefone;
- período de reset;
- quantidade de dias quando `n_days`;
- timezone IANA do reset;
- quais resultados consomem tentativa.

Defaults:

- pessoa: `8`;
- telefone: `4`;
- reset: `calendar_day`;
- timezone: `America/Sao_Paulo`;
- todos os resultados contam.

Ao menos um resultado deve consumir tentativa.

#### Etapa 7 — Revisão

Exibir em linguagem operacional:

- nome e status final;
- campanhas e distribuição, se houver;
- fonte geográfica e regiões;
- agenda semanal e timezone;
- datas especiais;
- limites por pessoa/telefone e período de reset;
- resultados contabilizados;
- lista dos recursos que serão criados.

O botão final distingue claramente **Salvar rascunho** de **Criar e publicar**.

### 11.3 Payload atômico de exemplo

Browser/BFF:

```http
POST /api/dialing/guided-configurations
```

Target Core:

```http
POST /v2/contact-supplier/guided-configurations
```

```json
{
  "name": "Cobrança nacional — padrão",
  "description": "Perfil criado pela experiência assistida",
  "campaigns": [
    {
      "flow_uuid": "11111111-1111-4111-8111-111111111111",
      "label": "Cobrança Cartões",
      "weight": 60,
      "hard_cap_percent": 70
    },
    {
      "flow_uuid": "22222222-2222-4222-8222-222222222222",
      "label": "Cobrança Conta",
      "weight": 40,
      "hard_cap_percent": 50
    }
  ],
  "geography": {
    "regional": true,
    "source": "contact_state",
    "regions": ["SP", "RJ"],
    "mailing_extra_field": ""
  },
  "schedule": {
    "weekday_start": "09:00",
    "weekday_end": "18:00",
    "saturday_enabled": true,
    "saturday_start": "10:00",
    "saturday_end": "14:00",
    "exceptions": [],
    "exception_ids": [
      "33333333-3333-4333-8333-333333333333"
    ],
    "new_exceptions": [
      {
        "name": "Aniversário municipal",
        "description": "Sem operação local",
        "date": "2026-11-20",
        "mode": "closed",
        "ranges": []
      }
    ]
  },
  "limits": {
    "max_attempts_per_person": 8,
    "max_attempts_per_phone": 4,
    "reset_period": {
      "type": "calendar_day"
    },
    "reset_timezone": "America/Sao_Paulo"
  },
  "counted_outcomes": [
    "ringing",
    "busy",
    "machine",
    "no_answer",
    "technical_failure",
    "invalid_number"
  ],
  "publish": true
}
```

Resposta contém:

```json
{
  "data": {
    "profile": {},
    "created": {
      "calendars": [],
      "attempt_policy": {},
      "attempt_limit": {}
    }
  }
}
```

## 12. Datas de exceção reutilizáveis

Tela própria para suporte operacional:

- inventário por nome, data e status;
- criação única do feriado/data especial;
- seleção de zero ou mais Perfis de Discagem por nome;
- aplicação aos calendários que compõem cada perfil;
- edição e atualização de vínculos;
- arquivamento e restauração;
- no wizard, seleção das mesmas exceções já cadastradas.

Endpoints:

| Operação | BFF | Target Core |
| --- | --- | --- |
| Listar/criar | `/api/dialing/calendar-exceptions` | `/v2/contact-supplier/calendar-exceptions` |
| Consultar/editar | `/api/dialing/calendar-exceptions/:id` | `/v2/contact-supplier/calendar-exceptions/:id` |
| Arquivar | `.../:id/archive` | `.../:id/archive` |
| Restaurar | `.../:id/restore` | `.../:id/restore` |

Exemplo:

```json
{
  "name": "Natal",
  "description": "Operação fechada",
  "date": "2026-12-25",
  "mode": "closed",
  "ranges": [],
  "profile_ids": [
    "44444444-4444-4444-8444-444444444444"
  ]
}
```

## 13. Simulador de Discagem

O simulador explica a decisão sem alterar contadores.

```http
POST /api/dialing/simulate
POST /v2/contact-supplier/simulate
```

Exemplo:

```json
{
  "profile_id": "44444444-4444-4444-8444-444444444444",
  "flow_uuid": "11111111-1111-4111-8111-111111111111",
  "phone": "5511999999999",
  "occurred_at": "2026-09-14T10:00:00-03:00",
  "contact_state": "SP",
  "address_state": null,
  "mailing_extra_state": null,
  "person_attempts": 2,
  "phone_attempts": 1,
  "previous_outcome": "busy"
}
```

Resposta relevante:

- `decision`: `allow`, `defer` ou `block`;
- `reason`;
- geografia resolvida;
- calendário e limite usados;
- tentativas restantes;
- próximo instante elegível;
- `trace` explicando a decisão;
- `attempt_count_delta` sempre `0`;
- `mutates_counters` sempre `false`.

A UI deve transformar `trace` em uma linha do tempo compreensível, sem esconder a representação técnica disponível para suporte.

## 14. API de Gestão de Discagem

Recursos válidos:

- `attempt-policies`;
- `attempt-limits`;
- `dialing-calendars`;
- `dial-profiles`.

Matriz genérica:

| Operação | BFF | Target Core |
| --- | --- | --- |
| Listar | `GET /api/dialing/:resource` | `GET /v2/contact-supplier/:resource` |
| Criar | `POST /api/dialing/:resource` | `POST /v2/contact-supplier/:resource` |
| Consultar | `GET /api/dialing/:resource/:id` | `GET /v2/contact-supplier/:resource/:id` |
| Editar | `PATCH /api/dialing/:resource/:id` | `PATCH /v2/contact-supplier/:resource/:id` |
| Publicar | `POST .../:id/publish` | `POST .../:id/publish` |
| Arquivar | `POST .../:id/archive` | `POST .../:id/archive` |
| Restaurar | `POST .../:id/restore` | `POST .../:id/restore` |
| Clonar | `POST .../:id/clone` | `POST .../:id/clone` |
| Consultar uso | `GET .../:id/usage` | `GET .../:id/usage` |

Listagens da referência usam `per_page=100`, mas a UI oficial deve implementar paginação verdadeira quando o volume exigir.

Exemplo de edição concorrente:

```bash
curl --fail-with-body \
  -X PATCH \
  "$TARGET_API_BASE/v2/contact-supplier/dial-profiles/$PROFILE_ID" \
  -H "Authorization: Bearer $TARGET_TOKEN" \
  -H "X-WORKSPACE-UUID: $WORKSPACE_UUID" \
  -H "X-User-UUID: $ACTOR_UUID" \
  -H 'Content-Type: application/json' \
  -H 'If-Match: "7"' \
  --data '{
    "name": "Nome revisado",
    "expected_revision": 7
  }'
```

### 14.1 Integração com o card do novo Discador

O card `send_with_dialer_handoff` não recebe uma regra completa embutida. Ele seleciona um Perfil de Discagem por `dial_profile_id`.

Fonte do dropdown dinâmico:

```http
GET /v2/contact-supplier/dial-profiles?page=1&per_page=100&status=published&active=true
```

Configuração visual:

- rótulo principal: `name`;
- valor persistido: `id`;
- mostrar somente perfis publicados e ativos;
- explicar quando a lista estiver vazia e oferecer link para criar/publicar um perfil;
- não permitir UUID nulo, inválido ou zero;
- todos os cards do novo discador dentro do mesmo flow devem selecionar o mesmo perfil.

No `SAVE`/`PUBLISH` do flow, o Target Core valida o perfil e sincroniza o vínculo efetivo do flow. O frontend não deve escrever diretamente na tabela de vínculos. Um perfil inexistente, arquivado, inativo ou sem revisão publicada deve aparecer como 422 no campo `<ref_id>.dial_profile_id`.

O card legado de discador continua existindo e não deve ser convertido automaticamente para esse contrato.

### 14.2 API de avaliação de restrições no runtime

A tela administra as listas; a avaliação consumida pelo card/runtime é outro contrato:

```http
POST /v2/contact-supplier/restrictions/evaluate
```

Ele recebe uma ou mais listas, escopo `current_channel` ou `person` e o sujeito a avaliar. Esta rota não deve ser chamada pelo browser para enumerar dados. Ela é registrada aqui apenas para deixar clara a separação entre **CRUD da UI** e **decisão operacional do fluxo**.

## 15. Listas de Restrição

### 15.1 Conceito

Lista que contém identificadores restritos por telefone, e-mail, documento ou outros campos autorizados. Possui vigência, motivo, referência externa e histórico de eventos.

Status da lista:

- `draft`;
- `active`;
- `archived`.

Status da entrada:

- `active`;
- `inactive`.

Arquivamento e inativação são lógicos. Não oferecer exclusão física comum.

### 15.2 Inventário

- alternância Cards / Tabela;
- busca e filtro por status;
- contadores resumidos;
- criação em rascunho;
- ativação depois da revisão;
- acesso ao detalhe 360.

### 15.3 Detalhe

Ao abrir uma lista:

- carregar cabeçalho, resumo e importações;
- **não carregar nem exibir registros automaticamente**;
- mostrar resumo acima da área de busca;
- exibir entradas somente depois de uma busca exata iniciada pelo usuário.

Resumo esperado:

- total;
- por telefone;
- por e-mail;
- por identificador;
- ativas;
- efetivas agora;
- expiradas;
- inativas.

A busca recebe o valor sensível no body de um POST, nunca na URL. Resultados devem ser mascarados quando exibidos.

### 15.4 Endpoints

Base BFF: `/api/dialing/restriction-lists`

Base Target: `/v2/contact-supplier/restriction-lists`

| Operação | Sufixo/método |
| --- | --- |
| Listar/criar listas | `GET|POST /` |
| Campos permitidos | `GET /field-options` |
| Consultar/editar | `GET|PATCH /:listId` |
| Ativar/arquivar/restaurar | `POST /:listId/activate|archive|restore` |
| Resumo | `GET /:listId/summary` |
| Busca exata | `POST /:listId/entries/search` |
| Criar entrada | `POST /:listId/entries` |
| Criar lote | `POST /:listId/entries/batch` — máximo 5.000 |
| Consultar/editar entrada | `GET|PATCH /:listId/entries/:entryId` |
| Eventos da entrada | `GET /:listId/entries/:entryId/events` |
| Inativar/reativar | `POST .../:entryId/deactivate|reactivate` |
| Listar/criar importações | `GET|POST /:listId/imports` |
| Consultar job | `GET /:listId/imports/:jobId` |
| Salvar mapping | `PUT /:listId/imports/:jobId/mapping` |
| Executar/cancelar | `POST .../:jobId/execute|cancel` |
| Relatório de erros | `GET .../:jobId/error-report` |

Busca exata:

```json
{
  "field_code": "phone",
  "value": "5511999999999",
  "status": "active"
}
```

Inclusão manual:

```json
{
  "field_code": "phone",
  "value": "5511999999999",
  "reason": "Solicitação do titular",
  "external_reference": "ticket-123",
  "effective_at": "2026-09-14T00:00:00-03:00",
  "expires_at": "2027-09-14T00:00:00-03:00"
}
```

### 15.5 Wizard de importação CSV

Jornada:

1. **Arquivo e estratégia** — escolher CSV e modo;
2. **Inspeção** — backend detecta encoding, delimitador e cabeçalho e devolve preview mascarado;
3. **Mapeamento** — cada coluna é ligada a um campo permitido ou marcada como ignorada;
4. **Validação** — revisar campos identificadores e metadados;
5. **Execução** — iniciar job assíncrono;
6. **Acompanhamento** — progresso e totais;
7. **Resultado** — resumo, relatório de erros e ações seguintes.

Regras:

- tamanho visível para o usuário: até 20 MiB;
- request multipart pode precisar de overhead adicional no BFF;
- enviar `Idempotency-Key` no upload;
- modos: `append_upsert` e `replace_active`;
- ao menos uma coluna precisa mapear para identificador;
- um campo de destino não pode ser escolhido duas vezes;
- coluna ignorada não possui destino;
- o upload não executa a importação automaticamente;
- salvar mapping e executar são ações separadas;
- jobs ativos recebem polling com backoff razoável;
- permitir cancelar quando o estado aceitar;
- oferecer nova tentativa somente com semântica segura.

Item de mapping:

```json
{
  "source_column": "telefone_cliente",
  "source_index": 0,
  "target_field_code": "phone",
  "is_ignored": false,
  "options": {}
}
```

Estados possíveis observados no fluxo:

- `uploaded`;
- `pending_mapping`;
- `validating`;
- `ready`;
- `queued`;
- `processing`;
- `completed`;
- `failed`;
- `cancelled`.

Durante o processamento, mostrar:

- total lido;
- válidos;
- rejeitados;
- ignorados;
- criados;
- atualizados;
- inativados pelo modo de substituição;
- percentual e status atual.

### 15.6 Privacidade

- não listar identificadores crus por padrão;
- não colocar valor pesquisado em query string, URL, analytics ou breadcrumb;
- mascarar resultados;
- limpar o valor sensível ao trocar de lista ou workspace;
- evitar logs client-side com request body;
- respeitar campos autorizados por `/field-options`;
- não armazenar CSV ou preview em `localStorage`.

## 16. Telecom — Troncos

### 16.1 Conceito e fronteira

A UI fala com o Target Core `/v2/telecom`. O Target Core atua como proxy/anti-corruption layer para a aplicação interna Pool. Não usar API Telecom legada V1 nem chamar o Pool diretamente do browser.

### 16.2 Inventário e visão 360

- Cards / Tabela;
- busca por nome;
- tipo do tronco;
- mecanismo de autenticação;
- DIDs;
- campanhas vinculadas;
- status `unknown`, `up` ou `down`;
- capacidades retornadas pela API;
- edição e exclusão somente quando `capabilities` permitirem.

Tipos:

- `standard` — gerenciável pela API;
- `metasip` — parte da estrutura é gerenciada pelo Pool; respeitar capacidades.

Autenticação:

- `user`: username, password, domain, outbound_proxy;
- `ip`: ip_address, mask, port.

A senha SIP é write-only: nunca volta no GET, nunca aparece preenchida e nunca deve ser inferida. Se não houver alteração de senha, omitir o campo.

### 16.3 Semântica de edição

- Alterar campos dentro do mesmo mecanismo: `PATCH`.
- Trocar autenticação `user` ↔ `ip`: `PUT` com representação completa.
- Ao trocar para `user`, uma nova senha é obrigatória.
- Ao receber 5xx após mutation, não repetir cegamente: a aplicação upstream pode ter aplicado a alteração antes de falhar a resposta. Consultar o recurso novamente e, se necessário, logs/correlação.

### 16.4 Endpoints

| Operação | BFF | Target Core |
| --- | --- | --- |
| Listar/criar | `GET|POST /api/telecom/trunks` | `GET|POST /v2/telecom/trunks` |
| Consultar/substituir/editar/excluir | `GET|PUT|PATCH|DELETE /api/telecom/trunks/:uuid` | mesmo sufixo em `/v2/telecom` |
| Status | `GET /api/telecom/trunks/:uuid/status` | `GET /v2/telecom/trunks/:uuid/status` |

Criação por usuário:

```json
{
  "name": "Tronco Exemplo",
  "description": "Ambiente autorizado",
  "authentication": {
    "type": "user",
    "username": "usuario_sip",
    "password": "<informada somente no envio>",
    "domain": "sip.exemplo.internal",
    "outbound_proxy": "sip:sip.exemplo.internal"
  },
  "dids": ["551112341234"]
}
```

Criação por IP:

```json
{
  "name": "Tronco IP Exemplo",
  "description": null,
  "authentication": {
    "type": "ip",
    "ip_address": "192.0.2.10",
    "mask": 32,
    "port": 5060
  },
  "dids": []
}
```

## 17. Telecom — Rotas

### 17.1 Inventário e visão 360

- Cards / Tabela;
- busca por nome;
- campanhas;
- gateways/troncos;
- regras por prefixo;
- distribuição por prioridade e peso;
- campos avançados retornados pela API aparecem como somente leitura;
- editar e excluir conforme `capabilities`;
- exclusão exige confirmação digitando o nome da rota.

Campos avançados de upstream, como `from_uri`, `request_uri`, `mt_tvalue`, `stopper` e `positus_uuid`, não devem ser fabricados ou descartados. Se a API não puder preservá-los com segurança, ela recusará substituição insegura.

### 17.2 Wizard de Rotas

Usar o mesmo padrão de experiência da Criação Assistida:

1. **Identificação** — nome;
2. **Campanhas** — seleção opcional por nome; já atribuídas ficam indisponíveis;
3. **Gateways** — tronco elegível, `strip` e `prefix`;
4. **Regras** — prefixo único e estado habilitado;
5. **Distribuição** — destinos, prioridade, peso e ANI;
6. **Revisão** — resumo completo antes de criar/substituir.

Regras:

- ao menos um gateway;
- ao menos uma regra;
- um tronco não aparece duas vezes como gateway;
- prefixos são únicos na rota;
- cada regra tem ao menos um destino;
- destino usa um tronco previamente escolhido como gateway;
- mesmo tronco não aparece duas vezes na mesma regra;
- menor número de prioridade vence;
- pesos `1..254` distribuem destinos empatados na mesma prioridade;
- ANI pode ser um número ou vários números separados por `;`.

### 17.3 Endpoints

| Operação | BFF | Target Core |
| --- | --- | --- |
| Listar/criar | `GET|POST /api/telecom/routes` | `GET|POST /v2/telecom/routes` |
| Consultar/substituir/editar/excluir | `GET|PUT|PATCH|DELETE /api/telecom/routes/:routeId` | mesmo sufixo em `/v2/telecom` |

Exemplo sanitizado:

```json
{
  "name": "Rota Brasil móvel",
  "campaigns": [
    "11111111-1111-4111-8111-111111111111"
  ],
  "gateways": [
    {
      "trunk_uuid": "55555555-5555-4555-8555-555555555555",
      "strip": 0,
      "prefix": ""
    }
  ],
  "rules": [
    {
      "prefix": "55",
      "enabled": true,
      "targets": [
        {
          "trunk_uuid": "55555555-5555-4555-8555-555555555555",
          "priority": 1,
          "weight": 100,
          "ani": "551112341234;551112341235"
        }
      ]
    }
  ]
}
```

## 18. Contexto de workspaces e flows

Endpoints do BFF:

- `GET /api/context/workspaces`;
- `GET /api/context/flows`.

Comportamento:

- Workspaces são exibidos por nome e selecionados no topo.
- O UUID continua disponível para suporte/cópia.
- Ao trocar workspace, invalidar caches e seleções do workspace anterior.
- Flows são carregados para combos por nome.
- A consulta otimizada usa `/v2/flow?for_list=true` e pagina enquanto necessário.
- Modos relevantes incluem `bot`, `aura` e `orchestration`, nos estados ativos/rascunho usados pela operação.
- Se a consulta de flows falhar, os demais CRUDs devem continuar disponíveis com aviso de degradação; não derrubar toda a UI.

O BFF de referência valida o workspace contra `/v1/workspaces` e mantém cache curto, atualmente 60 segundos.

## 19. Estados de tela e erros

Toda tela deve especificar:

| Estado | Comportamento esperado |
| --- | --- |
| Inicial | título, explicação e ação principal |
| Carregando | skeleton/indicador sem apagar toda a estrutura |
| Vazio | explicar por que não há dados e como criar/selecionar contexto |
| Parcial | manter recursos úteis e avisar qual dependência falhou |
| 401/403 | encerrar ação e orientar autenticação/permissão |
| 404 | recurso não existe mais; voltar ao inventário |
| 409 | conflito de revisão; não sobrescrever silenciosamente |
| 422 | erros por campo e resumo acionável |
| 429 | informar limitação e respeitar `Retry-After` quando houver |
| 5xx GET | permitir nova tentativa controlada |
| 5xx mutation | estado incerto; verificar por GET antes de oferecer repetição |
| Offline/timeout | preservar formulário e indicar indisponibilidade |
| Sucesso | confirmação e próximo passo natural |

Não manter loading infinito. O BFF de referência usa limites aproximados de 15 segundos para Target e a UI usa timeout próprio. A implementação oficial deve adotar os padrões globais do produto, sempre encerrando visualmente a espera.

## 20. Segurança

Requisitos mínimos:

- nenhum secret no bundle do frontend;
- autenticação do usuário pela plataforma oficial;
- autorização validada no backend para cada workspace;
- workspace vindo do contexto validado, nunca confiado isoladamente no header do browser;
- CSRF em mutations autenticadas por cookie;
- CORS/origin restritivo;
- `Cache-Control: no-store` para APIs e dados sensíveis;
- Content Security Policy adequada à UI oficial;
- sanitização de mensagens externas;
- nenhuma senha SIP em resposta, log ou estado persistido;
- nenhum identificador de blacklist em URL/analytics;
- confirmação forte antes de DELETE Telecom;
- idempotência em uploads e operações que a suportem;
- trilha de ator por `X-User-UUID` ou identidade equivalente da plataforma.

## 21. Desempenho e resiliência

- Paginar inventários; não carregar todos os flows completos para preencher um combo.
- Usar `for_list=true` para listagem amigável de flows.
- Cancelar requests obsoletos ao trocar workspace/filtro.
- Debounce em buscas textuais; busca sensível exata somente no submit.
- Não refazer automaticamente POST/PUT/PATCH/DELETE após timeout ou 5xx.
- Polling apenas para jobs ativos, com backoff e parada em estado terminal.
- Manter módulos independentes: falha na listagem de flows não bloqueia Listas de Restrição ou Telecom.
- Não buscar entradas de uma Lista de Restrição ao abrir o detalhe.
- Carregar status de troncos sob demanda ou em lote controlado; não criar tempestade de requests.

## 22. Checklist de aceite por jornada

### 22.1 Navegação

- [ ] Menu reproduz a hierarquia definida neste documento.
- [ ] Grupos expandem/recolhem com mouse, toque e teclado.
- [ ] Layout compacto funciona abaixo de 1280 px.
- [ ] Troca de tela não reaproveita filtros indevidamente.
- [ ] Workspace aparece por nome e cada request usa o UUID correto.
- [ ] Deep links oficiais restauram módulo e recurso.

### 22.2 Perfis e wizard

- [ ] Inventário alterna Cards/Tabela e persiste preferência.
- [ ] Visão 360 separa troca de referências de edição de conteúdo.
- [ ] Wizard começa somente após “Criar novo”.
- [ ] Campanhas são opcionais.
- [ ] Flows aparecem por nome; vinculados ficam indisponíveis com motivo.
- [ ] Geografia oferece país, estados/províncias reais e “Selecionar todos”.
- [ ] Wizard cria somente calendários por região; política e limite são compartilhados.
- [ ] Datas especiais existentes e novas podem ser usadas.
- [ ] Validações por etapa impedem avanço inconsistente.
- [ ] Submit atômico não deixa agregado parcial.
- [ ] 422 é exibido por campo.
- [ ] 409 não sobrescreve revisão concorrente.
- [ ] Sucesso abre visão 360 e simulador.

### 22.3 Listas de Restrição

- [ ] Inventário Cards/Tabela funciona.
- [ ] Detalhe abre sem carregar entradas.
- [ ] Resumo apresenta totais por tipo e estado.
- [ ] Busca exata usa POST e não vaza valor em URL.
- [ ] Resultados são mascarados.
- [ ] Inclusão manual e lifecycle funcionam.
- [ ] Upload aceita CSV dentro do limite e envia Idempotency-Key.
- [ ] Preview e mapping são exibidos antes da execução.
- [ ] Progresso e contadores são atualizados até estado terminal.
- [ ] Relatório de rejeitados é acessível.

### 22.4 Troncos

- [ ] Inventário, detalhe e status funcionam.
- [ ] Create suporta autenticação por usuário e IP.
- [ ] Edit permite alterar campos no mesmo mecanismo.
- [ ] Troca user/IP usa substituição completa.
- [ ] Senha SIP nunca é exibida nem reaproveitada.
- [ ] Capabilities de recursos gerenciados são respeitadas.
- [ ] Após 5xx de mutation, a UI consulta o estado antes de repetir.
- [ ] Exclusão exige confirmação forte.

### 22.5 Rotas

- [ ] Inventário e detalhe 360 funcionam.
- [ ] Wizard contém as seis etapas.
- [ ] Campanhas são opcionais e aparecem por nome.
- [ ] Apenas troncos elegíveis podem ser gateways.
- [ ] Prefixos e destinos duplicados são bloqueados.
- [ ] Prioridade, peso e ANI têm ajuda contextual.
- [ ] Campos avançados são preservados/somente leitura.
- [ ] Edição, substituição e exclusão respeitam capabilities e segurança.

## 23. Roteiro mínimo de homologação

Usar workspace de teste autorizado e dados descartáveis criados especificamente pela equipe. Não modificar troncos ou rotas preexistentes em produção durante a homologação.

1. Autenticar e selecionar workspace por nome.
2. Abrir cada módulo do menu e confirmar comportamento independente.
3. Criar um Perfil sem campanhas e salvar rascunho.
4. Criar outro perfil publicado com duas campanhas e pesos totalizando 100%.
5. Criar perfil regional com duas UFs e confirmar dois calendários mais fallback, uma política e um limite.
6. Criar data especial global e vinculá-la a um perfil.
7. Simular casos `allow`, `defer` e `block`; confirmar que nenhum contador muda.
8. Criar Lista de Restrição, ativar e inserir entrada manual descartável.
9. Confirmar que o detalhe não lista entradas até busca exata.
10. Importar CSV pequeno com uma linha válida e uma inválida; conferir mapping, progresso e relatório.
11. Criar tronco descartável autorizado em cada mecanismo de autenticação.
12. Editar o tronco descartável, consultar estado e excluir somente o recurso criado no teste.
13. Criar rota descartável pelo wizard, editar e excluir somente a rota do teste.
14. Repetir os principais cenários em viewport desktop e compacto.
15. Conferir ausência de secrets e dados crus em URL, console, analytics e armazenamento local.

## 24. Definition of Done da UI oficial

A migração da experiência é considerada concluída quando:

1. todas as jornadas deste documento estão disponíveis sob a navegação oficial;
2. o browser não conhece credenciais técnicas;
3. workspaces e flows são apresentados por nome;
4. o wizard de Perfil mantém a criação atômica e as sete etapas;
5. o wizard de importação mantém preview, mapping, job e relatório;
6. o wizard de Rotas mantém as seis etapas e suas invariantes;
7. concorrência, 422 e estado incerto de mutations possuem tratamento explícito;
8. privacidade de Listas de Restrição e senha SIP foi validada;
9. a suíte automatizada cobre regras críticas;
10. a homologação E2E passa em workspace autorizado;
11. documentação de suporte e ownership da nova UI está publicada;
12. a retirada da UI de referência só ocorre após aceite formal da substituta.

## 25. Limites e decisões que não devem ser revertidos

- Não recriar “Dial Rule” e “Orçamento de Spins” como conceitos separados.
- Não criar uma política/limite por UF; somente calendários podem variar por região.
- Não exigir campanha na criação do perfil.
- Não pedir UUID ao usuário como forma principal de seleção.
- Não editar internamente calendário/política/limite dentro da visão 360 do perfil.
- Não carregar registros crus de listas ao abrir o detalhe.
- Não chamar Pool/Telecom diretamente do browser.
- Não usar endpoints Telecom V1 para as novas telas.
- Não armazenar ou exibir senha SIP.
- Não repetir mutation automaticamente depois de resposta incerta.
- Não transformar falha da lista de flows em indisponibilidade completa do BFF/UI.
- Não aposentar a UI de referência antes de a UI oficial atingir paridade e ser homologada.

## 26. Referências internas relacionadas

- `PROJECT_BRAIN.md`
- `PROJECT_STEWARD.md`
- `docs/project-knowledge/DIALING_MANAGEMENT_UI_RUNBOOK.md`
- `docs/project-knowledge/DIALING_MANAGEMENT_V2_IMPLEMENTATION_PLAN.md`
- `docs/project-knowledge/KNOWN_RISKS.md`
- `docs/project-knowledge/INCIDENT_HISTORY.md`
- `docs/project-knowledge/MAINTENANCE_LOG.md`

Em caso de divergência, validar o comportamento no `main` atual da UI, nos schemas/routers do Target Core e no ambiente de referência. Atualizar este handoff junto com qualquer alteração material do contrato.
