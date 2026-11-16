# Workflow safety — producao 10.1.20.237

Estes drop-ins sao a fonte versionada dos limites de seguranca aplicados aos
workers e ao Beat de workflow no host canonico `10.1.20.237`.

Eles nao substituem as units base instaladas no servidor. Os templates
genericos diretamente em `systemctl/` continuam inadequados para esse host.

## Invariantes

- cinco workers `orch-celery-worker_01..05.service`;
- cada worker usa `--autoscale=8,2`;
- concorrencia agregada maxima de workflow: 40 processos;
- Beat principal usa lote 10 e intervalo de 5 segundos;
- filas, hostnames, fair scheduling e limites de reciclagem permanecem iguais
  aos da instalacao confirmada em producao.

## Instalar sem reiniciar

Executar como root a partir de `/etc/gohp/orch`:

```bash
scripts/prod_workflow_safety_237.sh install
scripts/prod_workflow_safety_237.sh check
```

`install` cria backup em `/etc/gohp/incident-backups/`, instala os drop-ins com
proprietario e permissoes explicitos e executa `systemctl daemon-reload`. Ele
deliberadamente nao reinicia servicos.

## Aplicar em runtime

Reiniciar os workers individualmente, validando banco e `pong` antes do
proximo. Reiniciar o Beat por ultimo somente quando a mudanca exigir reload de
seu processo.

Depois do rolling:

```bash
scripts/prod_workflow_safety_237.sh check
```

O check falha se um arquivo instalado divergir da fonte versionada, se uma
unit estiver inativa ou se a configuracao efetiva perder `8/2` ou `10/5s`.

## Rollback

Restaurar os arquivos do diretorio de backup informado por `install`, executar
`systemctl daemon-reload` e reiniciar em rolling somente as units afetadas.
Nunca remover drop-ins sem conhecer a configuracao efetiva das units base.
