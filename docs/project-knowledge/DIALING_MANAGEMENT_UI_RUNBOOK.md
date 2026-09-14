# Gestão de Extensões — UI operacional e runbook

Estado: `CONFIRMED` em 2026-09-13.

Esta aplicação começou como protótipo de regras de discagem, mas passou a
atender uma necessidade operacional contínua. Ela reúne atualmente:

- Perfis de Discagem;
- Listas de Restrição e importações CSV;
- Telecom / Troncos;
- reserva de navegação para Telecom / Rotas.

O nome de diretório `dialing-management-demo` permanece por compatibilidade da
instalação atual. Ele não significa que a aplicação ou seus dados sejam
descartáveis.

## Leia antes de alterar

1. O servidor `10.1.20.239` é destino de deploy, não fonte do código.
2. `/private/tmp` é staging descartável e nunca pode ser tratado como fonte da
   verdade.
3. O runtime global do host é Node `18.19.1` e é incompatível com o build atual.
4. A UI usa exclusivamente o Node `22.17.0` instalado em diretório próprio.
5. A UI não acessa banco. O navegador chama o BFF e o BFF chama APIs v2 do
   Target Core.
6. Toda alteração deve passar por lint, TypeScript, smoke do BFF e build local
   antes do deploy.
7. Cada deploy cria uma release física nova. Nunca construir diretamente em
   `current` nem alterar uma release anterior.

## Fonte oficial

- repositório privado: `https://github.com/GOHP-LAB/target-extensions-ui`;
- working copy local canônica:
  `/Users/deividazarias/google/prof/gohp/git/target-extensions-ui`;
- baseline importada da release ativa: commit `3035362`;
- branch principal: `main`, protegida contra force-push/exclusão e dependente do
  check `validate`;
- CI: Node `22.17.0`, `npm ci`, lint, TypeScript, smoke BFF e build.

A pasta local `dial-rule-studio-prototype`, qualquer staging sob `/private/tmp`
e as releases do `.239` são históricas/destinos de deploy e não substituem o
Git. Toda nova funcionalidade começa no `main` atualizado do repositório
oficial e segue seu próprio `AGENTS.md`, `PROJECT_BRAIN.md` e runbook.

## Arquitetura e fronteiras

```text
Navegador na rede interna/VPN
    -> Basic Auth + BFF 0.0.0.0:8300
    -> UI Vinext 127.0.0.1:8301
    -> Target Core CRUD no próprio host
    -> APIs /v2/contact-supplier e /v2/telecom
    -> serviços e schemas reais
```

- Unit da UI: `dialing-management-demo-ui.service`.
- Unit do BFF: `dialing-management-demo-bff.service`.
- Instalação: `/etc/gohp/dialing-management-demo`.
- Symlink ativo: `/etc/gohp/dialing-management-demo/current`.
- Configuração do BFF:
  `/etc/gohp/dialing-management-demo/dialing-management-demo.env`.
- Token Target server-side:
  `/etc/gohp/dialing-management-demo/secrets/target.token`.
- Segredos nunca entram no bundle, no Git, em comandos registrados ou na URL.

O BFF é fail-closed: somente rotas e métodos explicitamente permitidos podem
ser encaminhados. Mutações exigem mesma origem e o header CSRF da aplicação.

## Runtime Node canônico

Runtime obrigatório:

```text
/etc/gohp/dialing-management-demo/runtime/node-v22.17.0-linux-x64/bin/node
```

Verificação:

```bash
/etc/gohp/dialing-management-demo/runtime/node-v22.17.0-linux-x64/bin/node --version
```

Resultado esperado: `v22.17.0`.

### Comando canônico de build no servidor

```bash
/etc/gohp/dialing-management-demo/runtime/node-v22.17.0-linux-x64/bin/node \
  node_modules/vinext/dist/cli.js build
```

Não usar no servidor:

```bash
node --version
npm run build
node-v22/bin/node node-v22/lib/node_modules/npm/bin/npm-cli.js run build
```

Os dois últimos caminhos podem iniciar scripts com `#!/usr/bin/env node`; no
host atual isso volta ao Node 18 global. O sintoma confirmado é:

```text
SyntaxError: The requested module 'node:util' does not provide an export named 'styleText'
Node.js v18.19.1
```

A unit da UI também deve manter `ExecStart` com o caminho absoluto do Node 22.
Não instalar ou substituir o Node global para resolver este problema.

## Validação local obrigatória

Na fonte Git oficial, usar um Node 22 confirmado e executar:

```bash
node --version
npm run lint
npx tsc --noEmit
npm run smoke:bff
npm run build
```

O build local não substitui o build final da release Linux no `.239`.

## Navegador e inspeção visual canônicos

Não redescobrir nem substituir este procedimento a cada manutenção. Para
abrir, navegar, demonstrar ou inspecionar esta UI, usar o CLI oficial
`@playwright/cli`.

Instalação canônica:

```bash
npm install -g @playwright/cli@latest
```

Na estação atual, o prefixo global `/usr/local` pode exigir uma senha de
administrador diferente das credenciais dos servidores. Se a instalação acima
falhar com `EACCES`, instalar no prefixo do usuário e confirmar a resolução do
binário:

```bash
npm install -g --prefix /Users/deividazarias/.local @playwright/cli@latest
command -v playwright-cli
```

Abertura canônica:

```bash
playwright-cli open http://10.1.20.239:8300/
```

Comandos da mesma sessão podem então usar as referências do snapshot:

```bash
playwright-cli snapshot
playwright-cli click <ref>
```

A interface usa HTTP Basic. Não inserir usuário/senha na URL nem persistir
segredo no repositório. Para automação não interativa, usar `--config` apontando
para um JSON temporário fora do Git contendo exclusivamente
`browser.contextOptions.httpCredentials`; obter as credenciais do ambiente
server-side e remover o arquivo ao final. Exemplo sem valores reais:

```json
{
  "browser": {
    "contextOptions": {
      "httpCredentials": {
        "username": "<credencial-temporaria>",
        "password": "<credencial-temporaria>"
      }
    }
  }
}
```

```bash
playwright-cli close
playwright-cli open http://10.1.20.239:8300/ --config=/private/tmp/target-extensions-playwright.json
```

`.playwright-cli/`, snapshots, logs, perfis e arquivos de autenticação são
artefatos locais. Nunca commitá-los. Remover também qualquer configuração
temporária de autenticação após o uso.

## Deploy canônico no `10.1.20.239`

### Caminho feliz obrigatório

O procedimento padrão não é mais a sequência manual abaixo. A partir da
working copy do ORCH, execute o script versionado, que sempre empacota o
`origin/main` atualizado do repositório oficial da UI:

```bash
scripts/deploy_target_extensions_ui.sh \
  --label <assunto> \
  --smoke-path '<GET-read-only-da-funcionalidade>'
```

Exemplo:

```bash
scripts/deploy_target_extensions_ui.sh \
  --label calendar-exceptions \
  --smoke-path '/api/dialing/calendar-exceptions?per_page=1'
```

Antes de publicar, é possível validar resolução de fonte, commit, host e smoke
sem transferir ou alterar o servidor:

```bash
scripts/deploy_target_extensions_ui.sh \
  --label calendar-exceptions \
  --smoke-path '/api/dialing/calendar-exceptions?per_page=1' \
  --dry-run
```

O script:

1. atualiza e resolve exclusivamente `origin/main` da UI oficial;
2. ignora alterações da working copy ao criar o pacote Git;
3. transfere pacote e executor com checksum SHA-256;
4. eleva pelo caminho autorizado no host, `sudo su -c`, solicitando a senha
   interativamente sem colocá-la em argumento ou arquivo;
5. confirma serviços, symlink, espaço lógico e Node `22.17.0`;
6. cria uma release física a partir de `current/.`;
7. instala dependências com Node 22 somente se o lockfile mudou;
8. executa build limpo e valida sintaxe do BFF fora de `current`;
9. ativa o symlink atomicamente e reinicia somente UI/BFF;
10. valida health, o GET somente leitura informado, units e journal;
11. restaura automaticamente a release anterior se falhar após a ativação.

Usar os passos manuais das seções seguintes apenas para diagnóstico, recuperação
ou evolução do próprio script. Não redescobrir o procedimento em cada deploy.

### 1. Preflight somente leitura

```bash
readlink -f /etc/gohp/dialing-management-demo/current
systemctl show dialing-management-demo-ui.service \
  dialing-management-demo-bff.service \
  -p MainPID -p ActiveState -p SubState -p NRestarts --no-pager
df -h /etc/gohp/dialing-management-demo
```

Confirmar também o commit do Target Core quando a UI depende de contrato novo.
Preservar `.env`, tokens, releases e alterações operacionais existentes.

### 2. Criar uma release física

Use um nome explícito `YYYYMMDDTHHMMSS-<assunto>`. Primeiro crie o diretório;
depois copie o conteúdo apontado por `current`:

```bash
mkdir -p /etc/gohp/dialing-management-demo/releases/<nova-release>
cp -a /etc/gohp/dialing-management-demo/current/. \
  /etc/gohp/dialing-management-demo/releases/<nova-release>/
```

O `/.` é obrigatório. Não executar `cp -a current <destino>`: esse formato pode
preservar o symlink e fazer uma release antiga receber o novo build, eliminando
o rollback.

Aplicar somente os arquivos revisados na nova release e construir nela com o
comando absoluto do Vinext/Node 22.

### 3. Ativação atômica

Somente depois do build verde:

```bash
ln -s /etc/gohp/dialing-management-demo/releases/<nova-release> \
  /etc/gohp/dialing-management-demo/current.next
mv -Tf /etc/gohp/dialing-management-demo/current.next \
  /etc/gohp/dialing-management-demo/current
systemctl restart dialing-management-demo-ui.service
systemctl restart dialing-management-demo-bff.service
```

Antes de criar `current.next`, confirmar que ele não existe. Nunca construir em
`current` após trocar o symlink.

### 4. Smoke pós-deploy

Validar:

- `readlink -f current` aponta para a nova release;
- ambas as units estão `active/running`, com PID novo e zero restart;
- `GET /__health` do BFF responde `200` autenticado;
- uma rota real somente leitura do recurso alterado responde `200`;
- o journal não contém warning/error na janela do deploy;
- a tela abre via `playwright-cli open http://10.1.20.239:8300/`.

Mutações reais exigem alvo explicitamente destinado ao teste. Nunca repetir
`POST` automaticamente depois de timeout ou `5xx`, pois o efeito externo pode
ter sido consumado.

## Rollback

Reposicionar atomicamente `current` para a última release física conhecida e
reiniciar somente as duas units da UI/BFF. Dados criados pelas APIs não são
revertidos pela troca da UI e devem ser tratados separadamente.

Release anterior confiável ao deploy de 2026-09-13:

```text
/etc/gohp/dialing-management-demo/releases/20260913T105323-telecom-crud
```

Não usar `20260913T102705-telecom-trunks` como rollback independente: durante
uma implantação anterior, ela recebeu o mesmo build por cópia acidental do
symlink.

## Estado implantado em 2026-09-13

- release ativa:
  `/etc/gohp/dialing-management-demo/releases/20260913T125615-telecom-response-ui`;
- Target Core: merge da PR `#492`, commit implantado `0f39dcb`;
- UI/BFF: `active/running`, zero restart e sem warnings no journal;
- health BFF: `200`;
- `GET /api/telecom/trunks`: `200`;
- tela de MetaSip limita edição aos DIDs, conforme contrato real da Pool;
- nenhuma mutação de tronco foi executada durante este deploy.

## Homologação real de Telecom / Troncos em 2026-09-13

O caminho BFF -> Target Core v2 -> Pool foi exercitado no workspace
`ba7eb0ec-e565-447c-8c11-8f870cf72a60`:

- `GET /api/telecom/trunks`: `200`;
- `PATCH` vazio: `422` no envelope de validação esperado;
- `PATCH` de descrição no `Tronco Teste 0017`: `200`, confirmado por `GET`,
  seguido de restauração do valor original;
- tronco IP temporário: `POST 201`, `PUT 200` e confirmação por `GET`;
- remoção exclusiva do tronco temporário: `DELETE 204`, corpo vazio, seguida de
  `GET 404` e ausência no inventário final;
- Target Core CRUD, UI e BFF permaneceram `active/running`, com zero restarts e
  sem warning, exception ou traceback na janela do teste.

O `Tronco Teste 0017` terminou com nome, descrição, autenticação e DID
preservados; apenas `updated_at` avançou pela mutação e restauração. O smoke
visual via `playwright-cli` confirmou cinco troncos e nenhuma sobra do registro
temporário.

A inspeção do console também encontrou `404` não bloqueante nos arquivos de
fonte cujas URLs ainda apontam para uma release histórica e no `favicon.ico`.
Esse defeito visual/de empacotamento não afetou o CRUD e deve ser tratado em
patch isolado, sem ser confundido com regressão da integração Telecom.

## Consolidação Git concluída em 2026-09-13

A fonte limpa foi exportada da release ativa excluindo dependências, build,
caches, `.env` e metadados AppleDouble. O hash do arquivo de transferência foi
confirmado nas duas pontas. A instalação do lockfile e toda a regressão passaram
localmente; o primeiro CI oficial também passou em Node `22.17.0`.

O repositório possui governança própria e a working copy canônica está limpa.
O próximo risco técnico separado é a atualização coordenada das dependências:
`npm audit --omit=dev` apontou duas severidades altas e uma crítica alcançadas
por `next@16.2.6`, com correção indicada em `16.3.5`. Não atualizar por `--force`
nem misturar essa atividade com novas funcionalidades.
