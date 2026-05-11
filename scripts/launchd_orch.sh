#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TPL_DIR="${ROOT_DIR}/launchd"
AGENTS_DIR="${HOME}/Library/LaunchAgents"
RUN_DIR="${ROOT_DIR}/.runlogs/launchd"
mkdir -p "${AGENTS_DIR}" "${RUN_DIR}"

LAUNCH_UID="${LAUNCHCTL_UID:-$(stat -f %u "${HOME}")}"

LABELS=(
  "com.orch.api"
  "com.orch.celery.worker.legacy"
  "com.orch.celery.worker.fileapp"
  "com.orch.celery.beat.legacy"
  "com.orch.celery.worker.generate_file"
  "com.orch.celery.beat.generate_file"
)

template_for_label() {
  case "$1" in
    "com.orch.api") echo "com.orch.api.plist" ;;
    "com.orch.celery.worker.legacy") echo "com.orch.celery.worker.legacy.plist" ;;
    "com.orch.celery.worker.fileapp") echo "com.orch.celery.worker.fileapp.plist" ;;
    "com.orch.celery.beat.legacy") echo "com.orch.celery.beat.legacy.plist" ;;
    "com.orch.celery.worker.generate_file") echo "com.orch.celery.worker.generate_file.plist" ;;
    "com.orch.celery.beat.generate_file") echo "com.orch.celery.beat.generate_file.plist" ;;
    *) return 1 ;;
  esac
}

plist_path() {
  echo "${AGENTS_DIR}/$1.plist"
}

install_templates() {
  for label in "${LABELS[@]}"; do
    local template src dst
    template="$(template_for_label "${label}")"
    src="${TPL_DIR}/${template}"
    dst="$(plist_path "${label}")"
    sed "s#__REPO_ROOT__#${ROOT_DIR}#g" "${src}" > "${dst}"
    plutil -lint "${dst}" >/dev/null
    echo "[ok] installed ${dst}"
  done
}

bootstrap_all() {
  for label in "${LABELS[@]}"; do
    local dst
    dst="$(plist_path "${label}")"
    launchctl bootout "gui/${LAUNCH_UID}/${label}" >/dev/null 2>&1 || true
    if launchctl bootstrap "gui/${LAUNCH_UID}" "${dst}" >/dev/null 2>&1; then
      launchctl enable "gui/${LAUNCH_UID}/${label}" >/dev/null 2>&1 || true
      echo "[ok] bootstrapped ${label} (bootstrap)"
      continue
    fi

    if launchctl load -w "${dst}" >/dev/null 2>&1; then
      echo "[ok] bootstrapped ${label} (load -w fallback)"
      continue
    fi

    echo "[erro] falha ao iniciar ${label} via bootstrap e load -w."
    return 1
  done
}

bootout_all() {
  for label in "${LABELS[@]}"; do
    launchctl disable "gui/${LAUNCH_UID}/${label}" >/dev/null 2>&1 || true
    launchctl bootout "gui/${LAUNCH_UID}/${label}" >/dev/null 2>&1 || true
    echo "[ok] bootout ${label}"
  done
}

status_all() {
  local found_any=0
  for label in "${LABELS[@]}"; do
    echo "---- ${label}"
    if launchctl print "gui/${LAUNCH_UID}/${label}" >/dev/null 2>&1; then
      launchctl print "gui/${LAUNCH_UID}/${label}" | rg -n "state =|pid =|last exit code =|path =" || true
      found_any=1
      continue
    fi
    if launchctl print "user/${LAUNCH_UID}/${label}" >/dev/null 2>&1; then
      launchctl print "user/${LAUNCH_UID}/${label}" | rg -n "state =|pid =|last exit code =|path =" || true
      found_any=1
      continue
    fi
    if launchctl list | rg -n "${label}" >/dev/null 2>&1; then
      launchctl list | rg -n "${label}" || true
      found_any=1
      continue
    fi
    echo "not loaded"
  done
  echo "---- process check"
  pgrep -fal "uvicorn app.main:app|orch-worker-legacy@|orch-worker-fileapp@|orch-worker-gf@|celery .* beat" || true
  echo "---- api check"
  curl -s -o /dev/null -w "http=%{http_code}\n" "http://127.0.0.1:7777/health/celery" || true
  if [[ "${found_any}" -eq 0 ]]; then
    echo "[warn] launchctl não reportou labels; use process/api check acima."
  fi
}

diag() {
  echo "uid=$(id -u) launch_uid=${LAUNCH_UID}"
  echo "home=${HOME}"
  echo "--- print-disabled"
  launchctl print-disabled "gui/${LAUNCH_UID}" 2>/dev/null | rg -n "com\\.orch|disabled" || true
  echo "--- plist ownership"
  ls -l "${AGENTS_DIR}"/com.orch*.plist 2>/dev/null || true
}

logs_hint() {
  cat <<EOF
Logs:
  ${RUN_DIR}/api.out.log
  ${RUN_DIR}/api.err.log
  ${RUN_DIR}/worker_legacy.out.log
  ${RUN_DIR}/worker_legacy.err.log
  ${RUN_DIR}/worker_fileapp.out.log
  ${RUN_DIR}/worker_fileapp.err.log
  ${RUN_DIR}/beat_legacy.out.log
  ${RUN_DIR}/beat_legacy.err.log
  ${RUN_DIR}/worker_gf.out.log
  ${RUN_DIR}/worker_gf.err.log
  ${RUN_DIR}/beat_gf.out.log
  ${RUN_DIR}/beat_gf.err.log
EOF
}

usage() {
  cat <<EOF
Uso:
  scripts/launchd_orch.sh install     # instala templates em ~/Library/LaunchAgents
  scripts/launchd_orch.sh start       # bootstrap + enable dos 6 serviços
  scripts/launchd_orch.sh stop        # disable + bootout dos 6 serviços
  scripts/launchd_orch.sh restart     # stop + start
  scripts/launchd_orch.sh status      # status launchctl dos 6 serviços
  scripts/launchd_orch.sh uninstall   # stop + remove plists instalados
  scripts/launchd_orch.sh logs        # mostra caminhos de log
  scripts/launchd_orch.sh diag        # diagnóstico launchctl/uid
EOF
}

cmd="${1:-help}"
case "${cmd}" in
  install)
    install_templates
    ;;
  start)
    install_templates
    bootstrap_all
    ;;
  stop)
    bootout_all
    ;;
  restart)
    bootout_all
    install_templates
    bootstrap_all
    ;;
  status)
    status_all
    ;;
  uninstall)
    bootout_all
    for label in "${LABELS[@]}"; do
      rm -f "$(plist_path "${label}")"
    done
    echo "[ok] launch agents removidos."
    ;;
  logs)
    logs_hint
    ;;
  diag)
    diag
    ;;
  *)
    usage
    ;;
esac
