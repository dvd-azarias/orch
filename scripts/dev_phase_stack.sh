#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${ROOT_DIR}/.runlogs/phase_stack"
mkdir -p "${RUN_DIR}"

WORKSPACE_UUID="${WORKSPACE_UUID:-ba7eb0ec-e565-447c-8c11-8f870cf72a60}"
FLOW_A_UUID="${FLOW_A_UUID:-2cb9482a-131e-4b2a-8507-484745661836}"
FLOW_B_UUID="${FLOW_B_UUID:-fea492fb-9420-4690-ba09-bd73dca50717}"

API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-7777}"
API_BASE_URL="${API_BASE_URL:-http://${API_HOST}:${API_PORT}}"

DISPATCH_QUEUE="${CELERY_DISPATCH_QUEUE:-orch_dispatch_f5_local}"
EXECUTE_QUEUE="${CELERY_EXECUTE_QUEUE:-orch_execute_f5_local}"
HEARTBEAT_QUEUE="${CELERY_HEARTBEAT_QUEUE:-orch_heartbeat_f5_local}"
FILEAPP_INGEST_QUEUE="${CELERY_S3_FILES_INGEST_QUEUE:-orch_fileapp_ingest_f5_local}"
FILEAPP_PROCESS_QUEUE="${CELERY_SOURCE_LIST_INGEST_QUEUE:-orch_fileapp_source_list_f5_local}"
FILEAPP_MAILING_ASSOC_QUEUE="${CELERY_FILEAPP_MAILING_ASSOC_QUEUE:-orch_fileapp_mailing_assoc_f5_local}"
GF_RUN_QUEUE="${CELERY_GENERATE_FILE_RUN_QUEUE:-orch_component_generate_file_run_f5_local}"
GF_SCAN_QUEUE="${CELERY_GENERATE_FILE_SCAN_QUEUE:-orch_component_generate_file_scan_f5_local}"

VENV_ACTIVATE="${ROOT_DIR}/.venv/bin/activate"
if [[ ! -f "${VENV_ACTIVATE}" ]]; then
  echo "Virtualenv não encontrado em .venv/. Crie/ative antes de usar o script."
  exit 1
fi

start_proc() {
  local name="$1"
  local cmd="$2"
  local pid_file="${RUN_DIR}/${name}.pid"
  local log_file="${RUN_DIR}/${name}.log"

  if [[ -f "${pid_file}" ]] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
    echo "[skip] ${name} já está rodando (pid $(cat "${pid_file}"))."
    return 0
  fi

  (
    cd "${ROOT_DIR}"
    nohup bash -lc "source '${VENV_ACTIVATE}' && ${cmd}" >"${log_file}" 2>&1 &
    echo $! >"${pid_file}"
  )
  echo "[ok] ${name} iniciado (pid $(cat "${pid_file}"))."
}

stop_proc() {
  local name="$1"
  local pid_file="${RUN_DIR}/${name}.pid"
  if [[ ! -f "${pid_file}" ]]; then
    echo "[skip] ${name} sem pid file."
    return 0
  fi
  local pid
  pid="$(cat "${pid_file}")"
  if kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" || true
    sleep 1
    if kill -0 "${pid}" 2>/dev/null; then
      kill -9 "${pid}" || true
    fi
    echo "[ok] ${name} finalizado."
  else
    echo "[skip] ${name} já estava parado."
  fi
  rm -f "${pid_file}"
}

status_proc() {
  local name="$1"
  local pid_file="${RUN_DIR}/${name}.pid"
  if [[ -f "${pid_file}" ]] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
    echo "[up]   ${name} pid=$(cat "${pid_file}") log=${RUN_DIR}/${name}.log"
  else
    echo "[down] ${name}"
  fi
}

show_env() {
  cat <<EOF
WORKSPACE_UUID=${WORKSPACE_UUID}
FLOW_A_UUID=${FLOW_A_UUID}
FLOW_B_UUID=${FLOW_B_UUID}
API_BASE_URL=${API_BASE_URL}
CELERY_DISPATCH_QUEUE=${DISPATCH_QUEUE}
CELERY_EXECUTE_QUEUE=${EXECUTE_QUEUE}
CELERY_HEARTBEAT_QUEUE=${HEARTBEAT_QUEUE}
CELERY_S3_FILES_INGEST_QUEUE=${FILEAPP_INGEST_QUEUE}
CELERY_SOURCE_LIST_INGEST_QUEUE=${FILEAPP_PROCESS_QUEUE}
CELERY_FILEAPP_MAILING_ASSOC_QUEUE=${FILEAPP_MAILING_ASSOC_QUEUE}
CELERY_GENERATE_FILE_RUN_QUEUE=${GF_RUN_QUEUE}
CELERY_GENERATE_FILE_SCAN_QUEUE=${GF_SCAN_QUEUE}
EOF
}

wait_for_api() {
  for _ in $(seq 1 30); do
    local code
    code="$(curl -s -o /dev/null -w "%{http_code}" "${API_BASE_URL}/health/celery" || true)"
    if [[ "${code}" != "000" ]]; then
      echo "[ok] api respondeu em ${API_BASE_URL} (http ${code})."
      return 0
    fi
    sleep 1
  done
  echo "[erro] API não respondeu em ${API_BASE_URL} após 30s."
  return 1
}

wait_for_workers_ready() {
  local legacy_log="${RUN_DIR}/worker_legacy.log"
  local fileapp_log="${RUN_DIR}/worker_fileapp.log"
  local gf_log="${RUN_DIR}/worker_generate_file.log"
  for _ in $(seq 1 60); do
    if rg -q "orch-worker-legacy@.* ready\\." "${legacy_log}" 2>/dev/null \
      && rg -q "orch-worker-fileapp@.* ready\\." "${fileapp_log}" 2>/dev/null \
      && rg -q "orch-worker-gf@.* ready\\." "${gf_log}" 2>/dev/null; then
      echo "[ok] workers celery estão prontos."
      return 0
    fi
    sleep 1
  done
  echo "[erro] workers não ficaram prontos em 60s."
  return 1
}

start_all() {
  show_env
  start_proc "api" \
    "ORCH_QUEUE_PROFILE=f5_local CELERY_DISPATCH_QUEUE=${DISPATCH_QUEUE} CELERY_EXECUTE_QUEUE=${EXECUTE_QUEUE} CELERY_HEARTBEAT_QUEUE=${HEARTBEAT_QUEUE} CELERY_S3_FILES_INGEST_QUEUE=${FILEAPP_INGEST_QUEUE} CELERY_SOURCE_LIST_INGEST_QUEUE=${FILEAPP_PROCESS_QUEUE} CELERY_FILEAPP_MAILING_ASSOC_QUEUE=${FILEAPP_MAILING_ASSOC_QUEUE} uvicorn app.main:app --host ${API_HOST} --port ${API_PORT}"
  wait_for_api
  start_proc "worker_legacy" \
    "CELERY_ENABLED=true WORKFLOW_V2_ENABLED=true WORKFLOW_V2_EXECUTE_M2=true ORCH_QUEUE_PROFILE=f5_local \
CELERY_DISPATCH_QUEUE=${DISPATCH_QUEUE} CELERY_EXECUTE_QUEUE=${EXECUTE_QUEUE} CELERY_HEARTBEAT_QUEUE=${HEARTBEAT_QUEUE} \
CELERY_FILEAPP_INGEST_ENABLED=false CELERY_S3_FILES_INGEST_QUEUE=${FILEAPP_INGEST_QUEUE} CELERY_SOURCE_LIST_INGEST_QUEUE=${FILEAPP_PROCESS_QUEUE} \
CELERY_DISPATCH_WORKSPACE_UUID=${WORKSPACE_UUID} \
celery -A app.core.celery_app:celery_app worker --hostname=orch-celery-worker@_macbook_deivid_dev -n orch-worker-legacy@%h -Q ${DISPATCH_QUEUE},${EXECUTE_QUEUE},${HEARTBEAT_QUEUE} --without-mingle --without-gossip -l INFO"
  start_proc "worker_fileapp" \
    "CELERY_ENABLED=true WORKFLOW_V2_ENABLED=true WORKFLOW_V2_EXECUTE_M2=true ORCH_QUEUE_PROFILE=f5_local \
CELERY_FILEAPP_INGEST_ENABLED=true CELERY_S3_FILES_INGEST_QUEUE=${FILEAPP_INGEST_QUEUE} CELERY_SOURCE_LIST_INGEST_QUEUE=${FILEAPP_PROCESS_QUEUE} CELERY_FILEAPP_MAILING_ASSOC_QUEUE=${FILEAPP_MAILING_ASSOC_QUEUE} \
CELERY_GENERATE_FILE_ENABLED=false \
celery -A app.core.celery_app:celery_app worker --hostname=orch-celery-fileapp-worker@_macbook_deivid_dev -n orch-worker-fileapp@%h -Q ${FILEAPP_INGEST_QUEUE},${FILEAPP_PROCESS_QUEUE},${FILEAPP_MAILING_ASSOC_QUEUE} --without-mingle --without-gossip -l INFO"
  start_proc "beat_legacy" \
    "CELERY_ENABLED=true WORKFLOW_V2_ENABLED=true WORKFLOW_V2_EXECUTE_M2=true ORCH_QUEUE_PROFILE=f5_local \
CELERY_DISPATCH_QUEUE=${DISPATCH_QUEUE} CELERY_HEARTBEAT_QUEUE=${HEARTBEAT_QUEUE} \
CELERY_DISPATCH_WORKSPACE_UUID=${WORKSPACE_UUID} \
CELERY_GENERATE_FILE_ENABLED=false \
celery -A app.core.celery_app:celery_app beat --schedule=/tmp/orch-celerybeat-legacy-f5-local -l INFO"
  start_proc "worker_generate_file" \
    "CELERY_ENABLED=true WORKFLOW_V2_ENABLED=true WORKFLOW_V2_EXECUTE_M2=true ORCH_QUEUE_PROFILE=f5_local \
CELERY_GENERATE_FILE_ENABLED=true CELERY_GENERATE_FILE_WORKSPACE_UUID=${WORKSPACE_UUID} \
CELERY_GENERATE_FILE_RUN_QUEUE=${GF_RUN_QUEUE} CELERY_GENERATE_FILE_SCAN_QUEUE=${GF_SCAN_QUEUE} \
celery -A app.core.celery_app:celery_app worker --hostname=orch-celery-generate-file-worker@_macbook_deivid_dev -n orch-worker-gf@%h -Q ${GF_RUN_QUEUE},${GF_SCAN_QUEUE} --without-mingle --without-gossip -l INFO"
  start_proc "beat_generate_file" \
    "CELERY_ENABLED=true WORKFLOW_V2_ENABLED=true WORKFLOW_V2_EXECUTE_M2=true \
CELERY_BEAT_DISPATCH_ENABLED=false CELERY_BEAT_HEARTBEAT_ENABLED=false \
CELERY_GENERATE_FILE_ENABLED=true CELERY_GENERATE_FILE_WORKSPACE_UUID=${WORKSPACE_UUID} \
CELERY_GENERATE_FILE_RUN_QUEUE=${GF_RUN_QUEUE} CELERY_GENERATE_FILE_SCAN_QUEUE=${GF_SCAN_QUEUE} \
celery -A app.core.celery_app:celery_app beat --schedule=/tmp/orch-celerybeat-gf-f5-local -l INFO"
  wait_for_workers_ready
}

stop_all() {
  stop_proc "beat_generate_file"
  stop_proc "worker_generate_file"
  stop_proc "beat_legacy"
  stop_proc "worker_fileapp"
  stop_proc "worker_legacy"
  stop_proc "api"
}

status_all() {
  status_proc "api"
  status_proc "worker_legacy"
  status_proc "worker_fileapp"
  status_proc "beat_legacy"
  status_proc "worker_generate_file"
  status_proc "beat_generate_file"
}

smoke() {
  local count="${1:-5}"
  show_env
  wait_for_api
  wait_for_workers_ready
  for flow in "${FLOW_A_UUID}" "${FLOW_B_UUID}"; do
    echo "---- smoke flow=${flow} count=${count}"
    for i in $(seq 1 "${count}"); do
      local external_id
      local response
      external_id="smoke-${flow:0:8}-$(date +%s)-${i}"
      response="$(curl -sS -X POST \
        "${API_BASE_URL}/v1/orch/${WORKSPACE_UUID}/${flow}" \
        -H "Content-Type: application/json" \
        -d "{\"external_id\":\"${external_id}\",\"valor_recebido\":${i}}" || true)"
      if [[ -z "${response}" ]]; then
        echo "[erro] falha no POST flow=${flow} external_id=${external_id}"
        return 1
      fi
      printf '%s\n' "${response}" | python -m json.tool | sed -n '1,20p'
    done
  done
}

usage() {
  cat <<EOF
Uso:
  scripts/dev_phase_stack.sh start
  scripts/dev_phase_stack.sh stop
  scripts/dev_phase_stack.sh restart
  scripts/dev_phase_stack.sh status
  scripts/dev_phase_stack.sh smoke [count_por_fluxo]
  scripts/dev_phase_stack.sh env

Observações:
  - Script pensado para desenvolvimento local (pré-systemctl).
  - Usa filas dedicadas *_f5_local por padrão.
  - Escopo default de workspace: ${WORKSPACE_UUID}
EOF
}

cmd="${1:-help}"
case "${cmd}" in
  start) start_all ;;
  stop) stop_all ;;
  restart) stop_all; start_all ;;
  status) status_all ;;
  smoke) smoke "${2:-5}" ;;
  env) show_env ;;
  *) usage ;;
esac
