# ORCH AI Flow Builder — plano e gates

## Objetivo

Criar fluxos novos de `mode=orchestration` por conversa, inicialmente a partir
de texto e depois também de imagens. A experiência pertence à UI oficial de
Gestão de Extensões, mas a inteligência, o estado da conversa e a compilação
pertencem ao ORCH. O Target Core continua sendo a autoridade do catálogo, da
validação final e da persistência em `v2/flow`, para que o resultado abra no
canvas legado sem um segundo formato de fluxo.

Esta é uma funcionalidade estrutural que normalmente seria classificada como
`V2_ONLY`. Por decisão explícita de produto, sua primeira versão pertence ao
ORCH. Para não transformar o Alpha silenciosamente em outra V2, a entrega fica
isolada do runtime de sessões, desligada por padrão, restrita por workspace e
dividida nos gates abaixo.

## Invariantes

- somente `mode=orchestration` no primeiro rollout;
- nenhuma escrita direta do ORCH nas tabelas de flow do Target Core;
- componentes, branches e `next_task_allowed` vêm do catálogo real do
  workspace;
- a IA produz um `FlowPlan` estruturado; somente o compilador determinístico
  produz a `definition` legada;
- o Target Core continua executando sua validação autoritativa antes de salvar;
- criar somente rascunho; nunca publicar automaticamente;
- feature flag global, credencial dedicada e allowlist explícita de workspace;
- controle otimista de versão evita sobrescrever uma conversa alterada;
- imagens brutas não são persistidas nas tabelas do builder;
- logs e respostas nunca expõem bearer do Target ou credencial do provedor LLM;
- o runtime normal das sessões de orquestração não é alterado.

## Arquitetura

```text
Target Extensions UI
  -> BFF same-origin
    -> API dedicada do ORCH
       -> sessão, mensagens e FlowPlan no schema do workspace
       -> catálogo atual do Target Core
       -> planejador LLM (saída estruturada)
       -> compilador determinístico
       -> Target Core POST/PUT /v2/flow (rascunho)
         -> canvas legado existente
```

## Gates

### Gate 0 — Escopo e segurança — CONCLUÍDO

- [x] criação antes de edição;
- [x] orchestration antes de bot;
- [x] texto antes de imagem;
- [x] draft somente, sem publicação automática;
- [x] retorno obrigatório aos canários registrado.

### Gate 1 — Contratos e arquitetura — CONCLUÍDO

- [x] ORCH definido como owner da inteligência e do estado;
- [x] Target Core preservado como catálogo, validator e persistence boundary;
- [x] UI preservada como cliente, sem acesso ao banco;
- [x] inspeção da UI atual e do catálogo real do HighComm;
- [x] `FlowPlan` separado da `definition` legada.

### Gate 2 — Fundação segura no ORCH — CONCLUÍDO

- [x] migration workspace-local para sessões e mensagens;
- [x] autenticação dedicada, feature flag e allowlist fail-closed;
- [x] criar/ler sessão e registrar mensagens;
- [x] compilador determinístico com refs estáveis;
- [x] enforcement de catálogo, branch e `next_task_allowed`;
- [x] controle otimista de versão;
- [x] cliente read-only do catálogo Target Core;
- [x] testes unitários e PostgreSQL real com rollback;
- [x] catálogo real HighComm (27 cards) e compilação em memória sem escrita;
- [x] revisão/merge (`PR #226`, merge `5fdf592`);
- [x] migration e smoke controlados no HighComm.

### Gate 3 — MVP de criação por texto — CONCLUÍDO

- [x] planejador LLM com saída estritamente validada como `FlowPlan`;
- [x] conversa guiada para lacunas de configuração, sem adivinhar IDs;
- [x] preview estrutural e lista de pendências;
- [x] botão explícito **Criar rascunho**;
- [x] criação idempotente via Target Core e armazenamento de `flow_uuid`/
  checksum;
- [x] entrada **Criar fluxo com IA** na seção Orquestração da UI;
- [x] encaminhamento para o editor legado após a criação do draft;
- [x] BFF restrito, same-origin, com credencial dedicada e nenhuma rota de
  publicação;
- [x] defaults sensíveis do catálogo removidos antes do prompt do planejador;
- [x] smoke real sem escrita com catálogo HighComm + GPT-5 + compilação válida;
- [x] E2E texto -> conversa -> preview -> draft -> canvas.

O E2E real no HighComm criou somente o draft
`7c349597-29bd-41fa-aa34-c2d6b69b192b`, abriu o canvas legado com três cards e
quatro branches e preservou o status não publicado. Um segundo cenário
adversarial de discagem humana sem perfil, rota, equipe e canal permaneceu em
`needs_input`, sem chamada ao endpoint de draft. O gate continua fail-closed e
restrito pela allowlist.

### Gate 4 — Entrada por imagem — IMPLEMENTADO NO BRANCH, E2E/DEPLOY PENDENTES

- [x] upload com tipo/tamanho permitidos e descarte do binário após extração;
- [x] extração para intenção/FlowPlan, nunca diretamente para `definition`;
- [x] confirmação explícita de trechos ambíguos;
- [ ] E2E com diagrama conhecido e comparação estrutural.

### Gate 5 — Editar com IA e robustez — PENDENTE

- [ ] leitura do draft/checksum atual;
- [ ] proposta de diff antes de escrever;
- [ ] PUT com `expected_checksum` e tratamento de conflito;
- [ ] undo/auditoria de alterações;
- [ ] nenhuma mudança silenciosa em publicação ativa.

### Gate 6 — Rollout — PENDENTE

- [ ] stack local completa e regressão das fases homologadas;
- [ ] HighComm como primeiro workspace;
- [ ] métricas de erro, latência, conflito e rejeição do Target;
- [ ] habilitação de outros workspaces somente após evidência;
- [ ] bots continuam fora do escopo até decisão posterior.

### Gate final — Retorno à missão principal — OBRIGATÓRIO

- [ ] retomar a homologação dos cards no canário
  `81c0d014-3c4a-4393-80cb-57d97a00aed6` do HighComm;
- [ ] executar regressão no fluxo TRC
  `96cd32a7-8736-4312-a871-3e3eb3a3c5c7` quando aplicável;
- [ ] registrar onde o fluxo completo foi retomado e o próximo cenário ainda
  pendente.

## Rollback

Manter `ORCH_FLOW_BUILDER_ENABLED=false` remove imediatamente a superfície de
uso sem afetar sessões ou flows existentes. Código e migration são aditivos;
as tabelas podem permanecer sem consumidores durante rollback. Não remover
tabelas em produção como parte do rollback operacional.
