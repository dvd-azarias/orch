# Planejamento — Fase 5 (`generate_file`)

## Objetivo da fase 5

Portar para o `orch` o componente/card `generate_file`, com execução assíncrona compatível com o runtime atual (API + Celery), usando como base os exemplos:

- `**docs/commands-example/generate_file.py`
- `**docs/commands-services-example/generate_file_dispatch.py`
- `**docs/tsk-examples/generate_file_dispatch.py`

## Premissas operacionais (README + AGENTS)

- Validar em ambiente real com API/worker/beat ativos.
- Testes com rede externa (SFTP, webhook, broker) devem rodar fora da sandbox.
- Sem evidência externa (logs/arquivo no destino), não considerar validação concluída.
- Evitar fila global compartilhada; manter filas dedicadas do `orch`.
- Regra vigente: salvo pedido explícito, **nunca** reutilizar nomes de filas já existentes de outras aplicações no ambiente compartilhado.
- Escopo de testes funcionais desta fase:
  - workspace: `ba7eb0ec-e565-447c-8c11-8f870cf72a60`
  - flow principal de teste: `2cb9482a-131e-4b2a-8507-484745661836`

## Status de execução até a pausa (2026-05-09)

### O que foi implementado no código

- Card `generate_file` integrado ao runtime assíncrono.
- Execução adaptada para pipeline com dispatcher dedicado:
  - task scan: `app.tasks.component_generate_file.scan_due`
  - task run: `app.tasks.component_generate_file.run`
- Serviço de dispatch/processamento implementado no `orch` para ler job/buffer e executar envio SFTP.
- Filas dedicadas de `generate_file` separadas das filas de execução principal.
- Serviços `systemctl` separados para:
  - worker/beat legado do `orch`
  - worker/beat dedicado do `generate_file`

### O que foi validado (evidência real)

- Stack isolada subida com filas locais dedicadas (`*_f5_local`) para evitar backlog/ruído de fila compartilhada.
- `POST` real na API (porta `7788`) com `workspace` explícito:
  - 5 requests aceitas (`accepted=true`) no flow `2cb9482a-131e-4b2a-8507-484745661836`.
  - sessões retornadas: `685`, `686`, `687`, `688`, `689`.
- Registros foram persistidos em `generate_file_row_buffer` para essas sessões.

### Impasse técnico encontrado

- As tabelas `generate_file_job` / `generate_file_row_buffer` são compartilhadas com outra aplicação.
- Risco operacional confirmado: beats/workers de apps diferentes podem disputar/consumir os mesmos registros `pending`.
- Sinal observado em runtime:
  - scan do `generate_file` enfileirando jobs;
  - runs retornando `no_rows` em ciclos concorrentes;
  - comportamento sujeito a interferência externa por tabela compartilhada.
- Além disso, o job do teste estava em modo recorrente (`5m`) com `next_run_at` futuro, o que também explica pendências temporárias.

### Decisão pendente para retomada

- Definir arquitetura final para remover acoplamento entre aplicações:
  1. manter tabela compartilhada (alto risco operacional), ou
  2. migrar controle de job/buffer para armazenamento próprio do `orch` (recomendado).
- Recomendação técnica atual: **não depender de tabela compartilhada** para orquestração do `generate_file`.

### Decisão aplicada na retomada (2026-05-09)

- Decisão confirmada: criar cópia isolada das tabelas com prefixo `orch_` no schema do workspace.
- Tabelas adotadas pelo `orch`:
  - `orch_generate_file_job`
  - `orch_generate_file_row_buffer`
  - `orch_generate_file_dispatch_audit`
- Resultado esperado: eliminar interferência entre beats/workers de aplicações distintas.

## Escopo funcional

Implementar suporte a card `generate_file` para:

- montar conteúdo/arquivo com dados da sessão/contexto;
- gravar/enviar arquivo para destino configurado (SFTP);
- registrar resultado técnico e de negócio no estado da sessão;
- seguir padrão de execução assíncrona do runtime (`dispatch` + `advance_session`);
- manter comportamento idempotente e observável.

## Fora de escopo (nesta fase)

- novo provedor de armazenamento além de SFTP;
- UI/edição visual do componente;
- refatoração ampla de componentes legados;
- hardening avançado de throughput em massa (fica para fase posterior).

## Estratégia de implementação por etapas

### Etapa 1 — Descoberta técnica e contrato

- Mapear contrato do card nos exemplos (`payload`, parâmetros obrigatórios, saída esperada).
- Definir contrato final no runtime do `orch`:
  - entrada mínima;
  - variáveis de contexto aceitas;
  - formato de resultado e erros.
- Definir regra de persistência no `orch_sessions` (`data`, `api_call_last_result` ou campo equivalente do componente).

### Etapa 2 — Engine do componente

- Criar/adaptar serviço de execução `generate_file` no padrão dos comandos existentes.
- Implementar:
  - renderização de conteúdo;
  - nome/path de arquivo;
  - cliente SFTP;
  - upload e retorno de metadados (`remote_path`, tamanho, status).
- Garantir tratamento de erro com status técnico claro.

### Etapa 3 — Integração no runtime assíncrono

- Integrar componente ao fluxo de avanço de cards (`workflow_runtime_service` / equivalente).
- Garantir execução no pipeline assíncrono atual (sem bloquear resposta HTTP).
- Manter compatibilidade com controle de fila já isolado (`orch_dispatch`, `orch_execute`, `orch_heartbeat`).

### Etapa 4 — Observabilidade e segurança

- Logar eventos com correlação (`workspace_uuid`, `session_id`, `flow_uuid`, `card_uuid`).
- Sanitizar logs para não expor segredo/senha.
- Garantir que credenciais venham de configuração/card/segredo existente (sem hardcode no código).

### Etapa 5 — Validação funcional real

- Subir `API + worker + beat` fora da sandbox.
- Executar bateria de testes no workspace alvo e flow de teste.
- Comprovar criação/upload do arquivo no SFTP (`storage.otima.io:45884`) com evidência objetiva.

## Checklist técnico (implementação)

- [x] Contrato do card `generate_file` mapeado dos exemplos e aplicado ao runtime atual.
- [x] Serviço de execução `generate_file` implementado no `orch`.
- [x] Integração no avanço de workflow assíncrono concluída.
- [x] Tratamento de erro/status técnico básico implementado.
- [x] Logs operacionais mínimos do componente adicionados.
- [x] Sem segredos hardcoded em código.
- [x] Testes automatizados adicionados para fluxo de execução local do componente.
- [ ] Revisar arquitetura de persistência para remover dependência de tabela compartilhada.
- [x] Migration para tabelas isoladas `orch_generate_file_*` criada.
- [x] Serviço `generate_file_dispatch_service` apontado para tabelas `orch_generate_file_*`.

## Checklist de validação funcional (evidência real)

- [x] Sequência canônica de subida/retomada documentada em `scripts/dev_phase_stack.sh` e `README.md`.
- [x] API responde `accepted=true` para trigger do flow de teste.
- [ ] Sessão evolui de `state=0` para estado final esperado em cenário isolado sem interferência externa.
- [ ] Card `generate_file` executa fim-a-fim sem `no_rows` indevido em cenário isolado.
- [x] Arquivo aparece no destino SFTP em testes funcionais prévios desta fase.
- [x] Conteúdo/nome/path do arquivo foram validados em testes manuais prévios.
- [ ] Reexecução controlada sem efeito colateral indevido (idempotência) em arquitetura final.
- [x] Evidências principais registradas (requests aceitas, sessões, row_buffer, logs de worker/beat).

## Critério de saída da fase 5

A fase é considerada concluída quando:

1. O card `generate_file` está funcional no runtime assíncrono do `orch`.
2. O fluxo `2cb9482a-131e-4b2a-8507-484745661836` executa com sucesso no workspace `ba7eb0ec-e565-447c-8c11-8f870cf72a60`.
3. O arquivo é efetivamente gerado/copiado no SFTP com evidência objetiva.
4. Não houver dependência operacional insegura de tabela compartilhada com outra aplicação (ou houver isolamento formal aprovado).
5. Checklist técnico e funcional acima estão 100% marcados.

## Nota de credenciais e ambiente

- As credenciais do SFTP de teste já existem no ambiente/card; usar a fonte existente.
- Não registrar segredos em código, commit, logs ou documentação adicional.
