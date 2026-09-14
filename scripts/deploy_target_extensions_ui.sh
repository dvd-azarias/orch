#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEFAULT_UI_REPOSITORY="/Users/deividazarias/google/prof/gohp/git/target-extensions-ui"
readonly DEFAULT_REMOTE_HOST="10.1.20.239"
readonly DEFAULT_REMOTE_USER="deividazarias"
readonly DEFAULT_SMOKE_PATH="/api/context/workspaces"
readonly REMOTE_SCRIPT_PATH="/tmp/deploy-target-extensions-ui.sh"

deployment_staging_dir=""

usage() {
  cat <<'EOF'
Uso:
  scripts/deploy_target_extensions_ui.sh --label <assunto> [opcoes]

Publica exclusivamente o origin/main da UI oficial em uma release fisica nova
no 10.1.20.239. O acesso root remoto usa `sudo su -c` e solicita a senha de
forma interativa; nenhuma credencial entra no comando, no pacote ou no
repositorio.

Opcoes locais:
  --label <assunto>       Rotulo curto: letras minusculas, numeros e hifens.
  --repo <caminho>        Working copy oficial da UI.
  --host <host>           Host de destino (padrao: 10.1.20.239).
  --user <usuario>        Usuario SSH (padrao: deividazarias).
  --smoke-path <path>     GET autenticado apos deploy.
  --dry-run               Faz preflight/fetch, mas nao transfere nem publica.
  -h, --help              Exibe esta ajuda.

Exemplo:
  scripts/deploy_target_extensions_ui.sh \
    --label calendar-exceptions \
    --smoke-path '/api/dialing/calendar-exceptions?per_page=1'
EOF
}

fail() {
  printf 'ERRO: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "comando obrigatorio ausente: $1"
}

sha256_local() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    fail "shasum/sha256sum indisponivel"
  fi
}

cleanup_local_staging() {
  case "${deployment_staging_dir:-}" in
    /private/tmp/target-extensions-ui-deploy.*)
      [[ ! -d "$deployment_staging_dir" ]] || rm -rf -- "$deployment_staging_dir"
      ;;
  esac
}

remote_deploy() {
  local archive_path=""
  local expected_sha=""
  local commit_sha=""
  local release_label=""
  local smoke_path=""

  while (($#)); do
    case "$1" in
      --archive) archive_path="${2:-}"; shift 2 ;;
      --sha) expected_sha="${2:-}"; shift 2 ;;
      --commit) commit_sha="${2:-}"; shift 2 ;;
      --label) release_label="${2:-}"; shift 2 ;;
      --smoke-path) smoke_path="${2:-}"; shift 2 ;;
      *) fail "argumento remoto desconhecido: $1" ;;
    esac
  done

  [[ ${EUID} -eq 0 ]] || fail "a etapa remota deve executar via sudo"
  [[ "$archive_path" == /tmp/target-extensions-ui-*.tar ]] || fail "arquivo remoto fora do staging permitido"
  [[ "$expected_sha" =~ ^[0-9a-f]{64}$ ]] || fail "SHA-256 invalido"
  [[ "$commit_sha" =~ ^[0-9a-f]{40}$ ]] || fail "commit Git invalido"
  [[ "$release_label" =~ ^[a-z0-9][a-z0-9-]{0,47}$ ]] || fail "label invalido"
  [[ "$smoke_path" == /api/* ]] || fail "smoke-path deve iniciar por /api/"

  require_command sha256sum
  require_command systemctl
  require_command curl
  require_command tar

  local install_root="/etc/gohp/dialing-management-demo"
  local node_binary="${install_root}/runtime/node-v22.17.0-linux-x64/bin/node"
  local node_bin_dir="${install_root}/runtime/node-v22.17.0-linux-x64/bin"
  local environment_file="${install_root}/dialing-management-demo.env"
  local current_link="${install_root}/current"
  local next_link="${install_root}/current.next"
  local previous_release
  local release_stamp
  local release_path
  local build_log
  local available_kib
  local old_lock_sha="missing"
  local new_lock_sha="missing"
  local activated=0

  [[ -f "$archive_path" ]] || fail "pacote nao encontrado: $archive_path"
  [[ "$(sha256sum "$archive_path" | awk '{print $1}')" == "$expected_sha" ]] || fail "checksum do pacote divergiu"
  [[ -x "$node_binary" ]] || fail "Node 22 canonico nao encontrado"
  [[ -f "$environment_file" ]] || fail "arquivo de ambiente da UI nao encontrado"
  [[ -L "$current_link" ]] || fail "current nao e um symlink"
  [[ ! -e "$next_link" && ! -L "$next_link" ]] || fail "current.next ja existe; inspecione antes de prosseguir"

  systemctl is-active --quiet dialing-management-demo-ui.service || fail "servico UI nao esta ativo no preflight"
  systemctl is-active --quiet dialing-management-demo-bff.service || fail "servico BFF nao esta ativo no preflight"
  "$node_binary" --version | grep -qx 'v22.17.0' || fail "runtime Node inesperado"
  available_kib="$(df -Pk "$install_root" | awk 'NR == 2 {print $4}')"
  [[ "$available_kib" =~ ^[0-9]+$ ]] || fail "nao foi possivel medir o espaco livre"
  ((available_kib >= 2097152)) || fail "menos de 2 GiB livres para criar uma release segura"

  previous_release="$(readlink -f "$current_link")"
  release_stamp="$(date +%Y%m%dT%H%M%S)"
  release_path="${install_root}/releases/${release_stamp}-${release_label}-${commit_sha:0:7}"
  build_log="/var/tmp/target-extensions-ui-${commit_sha:0:7}-build.log"
  [[ ! -e "$release_path" ]] || fail "release de destino ja existe"

  printf 'Preflight OK\nCommit: %s\nRelease anterior: %s\nNova release: %s\n' \
    "$commit_sha" "$previous_release" "$release_path"

  mkdir "$release_path"
  cp -a "$current_link"/. "$release_path"/
  if [[ -f "$release_path/package-lock.json" ]]; then
    old_lock_sha="$(sha256sum "$release_path/package-lock.json" | awk '{print $1}')"
  fi
  tar -xf "$archive_path" -C "$release_path"
  if [[ -f "$release_path/package-lock.json" ]]; then
    new_lock_sha="$(sha256sum "$release_path/package-lock.json" | awk '{print $1}')"
  fi

  if [[ "$old_lock_sha" != "$new_lock_sha" ]]; then
    printf 'Lockfile alterado; instalando dependencias com Node 22...\n'
    (
      cd "$release_path"
      env PATH="${node_bin_dir}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" npm ci
    )
  fi

  if [[ -d "$release_path/.next" ]]; then
    mv "$release_path/.next" "$release_path/.next.pre-${commit_sha:0:7}"
  fi

  (
    cd "$release_path"
    "$node_binary" node_modules/vinext/dist/cli.js build >"$build_log" 2>&1
    "$node_binary" --check real-demo/bff.mjs
  ) || {
    tail -80 "$build_log" >&2 || true
    fail "build falhou; current permaneceu em $previous_release"
  }

  rollback_on_error() {
    local rollback_status=$?
    if ((activated)); then
      printf 'Falha apos ativacao; restaurando %s...\n' "$previous_release" >&2
      ln -s "$previous_release" "${install_root}/current.rollback"
      mv -Tf "${install_root}/current.rollback" "$current_link"
      systemctl restart dialing-management-demo-ui.service || true
      systemctl restart dialing-management-demo-bff.service || true
    fi
    exit "$rollback_status"
  }
  trap rollback_on_error ERR

  local restart_mark
  restart_mark="$(date -Is)"
  ln -s "$release_path" "$next_link"
  mv -Tf "$next_link" "$current_link"
  activated=1

  systemctl restart dialing-management-demo-ui.service
  sleep 3
  systemctl restart dialing-management-demo-bff.service
  sleep 3
  systemctl is-active --quiet dialing-management-demo-ui.service
  systemctl is-active --quiet dialing-management-demo-bff.service
  curl --fail --silent --show-error http://127.0.0.1:8300/__health >/dev/null

  set -a
  # shellcheck disable=SC1090
  source "$environment_file"
  set +a
  curl --fail --silent --show-error \
    -u "${DEMO_BASIC_AUTH_USER}:${DEMO_BASIC_AUTH_PASSWORD}" \
    "http://127.0.0.1:8300${smoke_path}" >/dev/null

  [[ "$(readlink -f "$current_link")" == "$release_path" ]]
  systemctl show dialing-management-demo-ui.service dialing-management-demo-bff.service \
    -p Id -p ActiveState -p SubState -p MainPID -p NRestarts --no-pager
  journalctl -u dialing-management-demo-ui.service -u dialing-management-demo-bff.service \
    --since "$restart_mark" --no-pager -p warning -n 80

  activated=0
  trap - ERR
  printf 'DEPLOY_OK commit=%s release=%s rollback=%s\n' \
    "$commit_sha" "$release_path" "$previous_release"
}

local_deploy() {
  local release_label=""
  local ui_repository="$DEFAULT_UI_REPOSITORY"
  local remote_host="$DEFAULT_REMOTE_HOST"
  local remote_user="$DEFAULT_REMOTE_USER"
  local smoke_path="$DEFAULT_SMOKE_PATH"
  local dry_run=0

  while (($#)); do
    case "$1" in
      --label) release_label="${2:-}"; shift 2 ;;
      --repo) ui_repository="${2:-}"; shift 2 ;;
      --host) remote_host="${2:-}"; shift 2 ;;
      --user) remote_user="${2:-}"; shift 2 ;;
      --smoke-path) smoke_path="${2:-}"; shift 2 ;;
      --dry-run) dry_run=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) fail "argumento desconhecido: $1" ;;
    esac
  done

  [[ "$release_label" =~ ^[a-z0-9][a-z0-9-]{0,47}$ ]] || fail "informe --label com letras minusculas, numeros e hifens"
  [[ "$smoke_path" == /api/* ]] || fail "smoke-path deve iniciar por /api/"
  [[ -d "$ui_repository/.git" ]] || fail "repositorio oficial nao encontrado: $ui_repository"
  require_command git
  require_command ssh
  require_command scp
  require_command tar
  require_command mktemp

  git -C "$ui_repository" fetch origin main
  local commit_sha
  local short_sha
  local dirty_count
  commit_sha="$(git -C "$ui_repository" rev-parse 'origin/main^{commit}')"
  short_sha="${commit_sha:0:7}"
  dirty_count="$(git -C "$ui_repository" status --porcelain | wc -l | tr -d ' ')"

  printf 'Fonte: %s\nRef: origin/main\nCommit: %s\nAlteracoes locais ignoradas pelo pacote: %s\n' \
    "$ui_repository" "$commit_sha" "$dirty_count"
  if ((dry_run)); then
    printf 'DRY_RUN_OK host=%s user=%s label=%s smoke=%s\n' \
      "$remote_host" "$remote_user" "$release_label" "$smoke_path"
    exit 0
  fi

  local staging_dir
  local archive_path
  local archive_sha
  local remote_archive
  local script_directory
  local script_path
  local remote_arguments
  local root_command
  local root_command_argument
  deployment_staging_dir="$(mktemp -d /private/tmp/target-extensions-ui-deploy.XXXXXX)"
  staging_dir="$deployment_staging_dir"
  trap cleanup_local_staging EXIT
  archive_path="${staging_dir}/target-extensions-ui-${short_sha}.tar"
  remote_archive="/tmp/target-extensions-ui-${short_sha}.tar"
  script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  script_path="${script_directory}/$(basename "${BASH_SOURCE[0]}")"

  git -C "$ui_repository" archive --format=tar --output="$archive_path" "$commit_sha"
  archive_sha="$(sha256_local "$archive_path")"
  printf 'Pacote: %s\nSHA-256: %s\n' "$archive_path" "$archive_sha"

  scp -q "$archive_path" "${remote_user}@${remote_host}:${remote_archive}"
  scp -q "$script_path" "${remote_user}@${remote_host}:${REMOTE_SCRIPT_PATH}"
  printf -v remote_arguments ' %q' \
    --remote \
    --archive "$remote_archive" \
    --sha "$archive_sha" \
    --commit "$commit_sha" \
    --label "$release_label" \
    --smoke-path "$smoke_path"

  # O sudoers do host permite elevar por `sudo su`, mas bloqueia executar o
  # script diretamente com sudo. Preserve cada argumento escapado e entregue
  # a linha completa como o unico argumento de `su -c`.
  printf -v root_command '/bin/bash %q%s' "$REMOTE_SCRIPT_PATH" "$remote_arguments"
  printf -v root_command_argument '%q' "$root_command"
  ssh -tt "${remote_user}@${remote_host}" \
    "sudo su -c ${root_command_argument}"
}

if [[ "${1:-}" == "--remote" ]]; then
  shift
  remote_deploy "$@"
else
  local_deploy "$@"
fi
