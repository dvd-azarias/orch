#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="systemctl/production-237"
TARGET_ROOT="/etc/systemd/system"
BACKUP_ROOT="/etc/gohp/incident-backups"
WORKER_UNITS=(
  orch-celery-worker_01.service
  orch-celery-worker_02.service
  orch-celery-worker_03.service
  orch-celery-worker_04.service
  orch-celery-worker_05.service
)
BEAT_UNIT="orch-celery-beat.service"
DROPIN_FILES=(
  "${BEAT_UNIT}.d/zz-incident-dispatch-safe.conf"
  "${WORKER_UNITS[0]}.d/zz-db-safety-autoscale.conf"
  "${WORKER_UNITS[1]}.d/zz-db-safety-autoscale.conf"
  "${WORKER_UNITS[2]}.d/zz-db-safety-autoscale.conf"
  "${WORKER_UNITS[3]}.d/zz-db-safety-autoscale.conf"
  "${WORKER_UNITS[4]}.d/zz-db-safety-autoscale.conf"
)

fail() {
  echo "[fail] $*" >&2
  exit 1
}

require_repo_root() {
  [[ -f "${SOURCE_ROOT}/README.md" ]] || fail "execute a partir da raiz de /etc/gohp/orch"
}

install_dropins() {
  require_repo_root
  [[ "$(id -u)" == "0" ]] || fail "install exige root"

  local timestamp backup_dir relative source target
  timestamp="$(date +%Y%m%dT%H%M%S)"
  backup_dir="${BACKUP_ROOT}/prod-workflow-safety-${timestamp}"
  install -d -m 0700 "${backup_dir}"

  for relative in "${DROPIN_FILES[@]}"; do
    source="${SOURCE_ROOT}/${relative}"
    target="${TARGET_ROOT}/${relative}"
    [[ -f "${source}" ]] || fail "fonte ausente: ${source}"
    if [[ -f "${target}" ]]; then
      install -D -m 0600 "${target}" "${backup_dir}/${relative}"
    fi
    install -D -o root -g root -m 0644 "${source}" "${target}"
  done

  systemctl daemon-reload
  echo "[ok] drop-ins instalados sem restart"
  echo "[ok] backup: ${backup_dir}"
}

check_dropins() {
  require_repo_root

  local relative source target unit state exec_start beat_environment
  for relative in "${DROPIN_FILES[@]}"; do
    source="${SOURCE_ROOT}/${relative}"
    target="${TARGET_ROOT}/${relative}"
    [[ -f "${target}" ]] || fail "drop-in ausente: ${target}"
    cmp -s "${source}" "${target}" || fail "drop-in divergiu: ${target}"
  done

  for unit in "${WORKER_UNITS[@]}"; do
    state="$(systemctl show "${unit}" -p ActiveState --value)"
    [[ "${state}" == "active" ]] || fail "unit inativa: ${unit} (${state})"
    exec_start="$(systemctl show "${unit}" -p ExecStart --value)"
    [[ "${exec_start}" == *"--autoscale=8,2"* ]] || fail "autoscale inseguro: ${unit}"
    [[ "${exec_start}" == *"--prefetch-multiplier=1"* ]] || fail "prefetch divergente: ${unit}"
  done

  state="$(systemctl show "${BEAT_UNIT}" -p ActiveState --value)"
  [[ "${state}" == "active" ]] || fail "unit inativa: ${BEAT_UNIT} (${state})"
  beat_environment="$(systemctl show "${BEAT_UNIT}" -p Environment --value)"
  [[ "${beat_environment}" == *"CELERY_DISPATCH_INTERVAL_SECONDS=5"* ]] || fail "intervalo do Beat divergiu"
  [[ "${beat_environment}" == *"CELERY_DISPATCH_BATCH_SIZE=10"* ]] || fail "lote do Beat divergiu"

  echo "[ok] cinco workers ativos com autoscale agregado maximo 40"
  echo "[ok] Beat ativo com dispatch 10/5s"
}

usage() {
  echo "Uso: scripts/prod_workflow_safety_237.sh <install|check>"
}

case "${1:-}" in
  install) install_dropins ;;
  check) check_dropins ;;
  *) usage; exit 2 ;;
esac
