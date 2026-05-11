# Systemd templates (produção)

> Observação: estes arquivos são para Linux com `systemd` (`systemctl`).
> Para macOS, usar `launchd` conforme `launchd/README.md`.

Arquivos:
- `systemctl/orch-api.service`
- `systemctl/orch-celery-worker.service`
- `systemctl/orch-celery-fileapp-worker.service`
- `systemctl/orch-celery-beat.service`
- `systemctl/orch-celery-generate-file-worker.service`
- `systemctl/orch-celery-generate-file-beat.service`

## Pré-requisitos

- Projeto em `/opt/orch`
- Virtualenv em `/opt/orch/.venv`
- Usuário/grupo `orch`
- Arquivo de ambiente em `/etc/orch/orch.env` (copiar variáveis do `.env` do projeto)
  - manter `ORCH_QUEUE_PROFILE=prod`
- Diretório para scheduler do beat:
  - `sudo mkdir -p /var/lib/orch && sudo chown orch:orch /var/lib/orch`

## Instalação dos serviços

```bash
sudo cp systemctl/orch-api.service /etc/systemd/system/
sudo cp systemctl/orch-celery-worker.service /etc/systemd/system/
sudo cp systemctl/orch-celery-fileapp-worker.service /etc/systemd/system/
sudo cp systemctl/orch-celery-beat.service /etc/systemd/system/
sudo cp systemctl/orch-celery-generate-file-worker.service /etc/systemd/system/
sudo cp systemctl/orch-celery-generate-file-beat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orch-api orch-celery-worker orch-celery-fileapp-worker orch-celery-beat orch-celery-generate-file-worker orch-celery-generate-file-beat
```

## Comandos úteis

```bash
sudo systemctl status orch-api orch-celery-worker orch-celery-fileapp-worker orch-celery-beat orch-celery-generate-file-worker orch-celery-generate-file-beat
sudo journalctl -u orch-api -f
sudo journalctl -u orch-celery-worker -f
sudo journalctl -u orch-celery-fileapp-worker -f
sudo journalctl -u orch-celery-beat -f
sudo journalctl -u orch-celery-generate-file-worker -f
sudo journalctl -u orch-celery-generate-file-beat -f
sudo systemctl restart orch-api orch-celery-worker orch-celery-fileapp-worker orch-celery-beat orch-celery-generate-file-worker orch-celery-generate-file-beat
```

## Topologia recomendada (fase atual)

- `orch-api`: API FastAPI.
- `orch-celery-worker`: filas de workflow (`orch_dispatch`, `orch_execute`, `orch_heartbeat`).
- `orch-celery-fileapp-worker`: filas FileApp (`orch_fileapp_ingest_events`, `orch_fileapp_source_list_ingest`).
- `orch-celery-beat`: beat do workflow (dispatch/heartbeat).
- `orch-celery-generate-file-worker`: worker do componente `generate_file`.
- `orch-celery-generate-file-beat`: beat do `generate_file`.

Essa separação evita competição de consumo com outras aplicações e melhora visibilidade no Flower.

## Hostnames padronizados (Flower)

Nos workers de produção deste projeto, usar `--hostname` explícito:

- `orch-celery-worker@136_01`
- `orch-celery-fileapp-worker@136_01`
- `orch-celery-generate-file-worker@136_01`

Benefício: filtros estáveis no Flower por responsabilidade e host.

## Ajustes recomendados de performance

- `--concurrency`: começar com `2 x vCPU` e ajustar por métricas reais.
- `--prefetch-multiplier=1 -Ofair`: melhor distribuição quando há tasks heterogêneas.
- `--max-tasks-per-child` e `--max-memory-per-child`: mitigam degradação de longo prazo.
- Escalar horizontalmente com mais workers antes de aumentar demais um único processo.
- Para API, ajustar `--workers` conforme CPU e perfil de latência (começar em `vCPU` ou `2 x vCPU`).
