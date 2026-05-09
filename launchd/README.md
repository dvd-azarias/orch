# macOS `launchd` (equivalente ao `systemctl`)

Este diretório contém templates `.plist` para manter a stack local do `orch` de pé no macOS:

- `com.orch.api.plist`
- `com.orch.celery.worker.legacy.plist`
- `com.orch.celery.beat.legacy.plist`
- `com.orch.celery.worker.generate_file.plist`
- `com.orch.celery.beat.generate_file.plist`

## Objetivo operacional

Manter as fases já homologadas sempre ativas em DEV local:

- Fase 4: API + worker/beat legado.
- Fase 5: worker/beat `generate_file`.

As fases são encadeadas: ao evoluir fase 6/7/8, as fases anteriores devem permanecer de pé para teste real.

## Controle dos serviços

Script:

- `scripts/launchd_orch.sh install`
- `scripts/launchd_orch.sh start`
- `scripts/launchd_orch.sh status`
- `scripts/launchd_orch.sh restart`
- `scripts/launchd_orch.sh stop`
- `scripts/launchd_orch.sh uninstall`
- `scripts/launchd_orch.sh logs`

## Regra de manutenção (obrigatória)

Sempre que houver intervenção de código em API, workers, beats, filas ou configuração:

1. reiniciar os serviços afetados (`scripts/launchd_orch.sh restart`);
2. validar `status`;
3. executar smoke real (`scripts/dev_phase_stack.sh smoke 5` ou equivalente).

