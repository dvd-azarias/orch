#!/usr/bin/env bash
set -euo pipefail

UNITS=(
  orch-api
  orch-celery-worker
  orch-celery-fileapp-worker
  orch-celery-beat
  orch-celery-generate-file-worker
  orch-celery-generate-file-beat
)

usage() {
  cat <<EOF
Uso:
  scripts/systemd_orch.sh install [repo_path] [env_path]
  scripts/systemd_orch.sh start
  scripts/systemd_orch.sh stop
  scripts/systemd_orch.sh restart
  scripts/systemd_orch.sh status
  scripts/systemd_orch.sh logs <unit>

Exemplos:
  scripts/systemd_orch.sh install /opt/orch /etc/orch/orch.env
  scripts/systemd_orch.sh start
  scripts/systemd_orch.sh status
  scripts/systemd_orch.sh logs orch-celery-fileapp-worker
EOF
}

install_units() {
  local repo_path="${1:-/opt/orch}"
  local env_path="${2:-/etc/orch/orch.env}"

  sudo mkdir -p /etc/orch /var/lib/orch
  sudo cp systemctl/*.service /etc/systemd/system/
  sudo cp systemctl/orch.env.example "${env_path}" || true
  sudo chown -R orch:orch /var/lib/orch || true

  for unit in /etc/systemd/system/orch-*.service; do
    sudo sed -i.bak "s#/opt/orch#${repo_path}#g" "${unit}"
    sudo sed -i.bak "s#/etc/orch/orch.env#${env_path}#g" "${unit}"
    sudo rm -f "${unit}.bak"
  done

  sudo systemctl daemon-reload
  echo "[ok] units instaladas em /etc/systemd/system"
}

start_units() {
  sudo systemctl enable --now "${UNITS[@]}"
}

stop_units() {
  sudo systemctl disable --now "${UNITS[@]}" || true
}

status_units() {
  sudo systemctl status "${UNITS[@]}" --no-pager
}

logs_unit() {
  local unit="${1:-}"
  if [[ -z "${unit}" ]]; then
    echo "Informe a unit. Ex: orch-celery-fileapp-worker"
    exit 1
  fi
  sudo journalctl -u "${unit}" -f
}

cmd="${1:-help}"
case "${cmd}" in
  install) install_units "${2:-/opt/orch}" "${3:-/etc/orch/orch.env}" ;;
  start) start_units ;;
  stop) stop_units ;;
  restart) stop_units; start_units ;;
  status) status_units ;;
  logs) logs_unit "${2:-}" ;;
  *) usage ;;
esac
