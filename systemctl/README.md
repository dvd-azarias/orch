# Systemd templates (produção)

Arquivos:
- `systemctl/orch-api.service`
- `systemctl/orch-celery-worker.service`
- `systemctl/orch-celery-beat.service`

## Pré-requisitos

- Projeto em `/opt/orch`
- Virtualenv em `/opt/orch/.venv`
- Usuário/grupo `orch`
- Arquivo de ambiente em `/etc/orch/orch.env` (copiar variáveis do `.env` do projeto)
- Diretório para scheduler do beat:
  - `sudo mkdir -p /var/lib/orch && sudo chown orch:orch /var/lib/orch`

## Instalação dos serviços

```bash
sudo cp systemctl/orch-api.service /etc/systemd/system/
sudo cp systemctl/orch-celery-worker.service /etc/systemd/system/
sudo cp systemctl/orch-celery-beat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orch-api orch-celery-worker orch-celery-beat
```

## Comandos úteis

```bash
sudo systemctl status orch-api orch-celery-worker orch-celery-beat
sudo journalctl -u orch-api -f
sudo journalctl -u orch-celery-worker -f
sudo journalctl -u orch-celery-beat -f
sudo systemctl restart orch-api orch-celery-worker orch-celery-beat
```

## Ajustes recomendados de performance

- `--concurrency`: começar com `2 x vCPU` e ajustar por métricas reais.
- `--prefetch-multiplier=1 -Ofair`: melhor distribuição quando há tasks heterogêneas.
- `--max-tasks-per-child` e `--max-memory-per-child`: mitigam degradação de longo prazo.
- Escalar horizontalmente com mais workers antes de aumentar demais um único processo.
- Para API, ajustar `--workers` conforme CPU e perfil de latência (começar em `vCPU` ou `2 x vCPU`).
